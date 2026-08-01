#!/usr/bin/env python3
"""FunnyPilot v3.2.6e — longitudinal planner, rewritten single-authority.

What we expect from an autonomous vehicle's longitudinal control, in strict
priority order:

  1. SAFETY. The MPC owns the safe-following problem — headway, braking
     envelope, danger-zone constraint, FCW. Nothing downstream may weaken or
     delay its braking. Heuristics may only shape its INPUTS (cruise speed,
     headway), never clamp its output. The 3.2.5st FollowingControllerV2
     accel/jerk overrides (whose 0.5 m/s^3 jerk cap could delay a 3 m/s^2
     braking demand by SECONDS while tiers escalated) are gone.
  2. COMFORT. Bounded acceleration (A_CRUISE_MAX table, turn limiting) and
     bounded jerk, applied in exactly ONE place (long_shaping.AccelJerkShaper)
     with asymmetric limits: throttle is applied gently, braking is slew-
     limited only as much as the demanded deceleration allows, FCW bypasses
     shaping entirely.
  3. PREDICTABILITY. Set speed means set speed — the hidden 0.9x cruise
     offset is removed. No time-boxed personality gas gates, no lead-cap
     blending, no stacked output filters. The command is a deterministic
     function of the plan.
  4. ROBUSTNESS. Lead flicker and departures are handled in the SPEED domain
     (long_shaping.LeadGrace): the cap is floored at v_ego, so it can hold
     the car back after a lead drops but can never brake it.
"""
import numpy as np

import cereal.messaging as messaging
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from cereal import log
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, get_accel_from_plan
from openpilot.selfdrive.controls.lib.long_shaping import AccelJerkShaper, LeadGrace
from openpilot.selfdrive.controls.lib.turn_limit import limit_accel_in_turns, predicted_lat_accel
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.common.swaglog import cloudlog

from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlannerSP

# FunnyPilot: reduced max acceleration (70% of stock) for smoother driving
A_CRUISE_MAX_VALS = [1.12, 0.84, 0.56, 0.42]  # 70% of [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MAX_BP = [0., 10.0, 25., 40.]
CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]
ALLOW_THROTTLE_THRESHOLD = 0.4
MIN_ALLOW_THROTTLE_SPEED = 2.5

# FunnyPilot v3.3.3st: hidden speed governor. Not shown anywhere in the
# UI or driver-facing state - it shaves the MPC's target speed before the
# controls layer ever sees it. Since v3.3.4 it applies to the cruise
# ceiling at all times, lead or no lead: braking for a slower lead is
# unaffected (the MPC's lead constraint sits below the ceiling), but a
# lead can no longer pull the car above the governed speed.
HIDDEN_CRUISE_OFFSET = 0.93

# Up-jerk (throttle application) by personality, m/s^3
JERK_UP_AGGRESSIVE = 2.5
JERK_UP_STANDARD = 1.8
JERK_UP_RELAXED = 1.4

# Lookup table for turns
def get_max_accel(v_ego):
  return np.interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)

def get_coast_accel(pitch):
  return np.sin(pitch) * -5.65 - 0.3  # fitted from data using xx/projects/allow_throttle/compute_coast_accel.py

def get_jerk_up(personality):
  if personality == log.LongitudinalPersonality.aggressive:
    return JERK_UP_AGGRESSIVE
  elif personality == log.LongitudinalPersonality.relaxed:
    return JERK_UP_RELAXED
  return JERK_UP_STANDARD

class LongitudinalPlanner(LongitudinalPlannerSP):
  def __init__(self, CP, CP_SP, init_v=0.0, init_a=0.0, dt=DT_MDL):
    self.CP = CP
    self.mpc = LongitudinalMpc(dt=dt)
    LongitudinalPlannerSP.__init__(self, self.CP, CP_SP, self.mpc)
    self.fcw = False
    self.dt = dt
    self.allow_throttle = True

    self.a_desired = init_a
    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, self.dt)
    self.prev_accel_clip = [ACCEL_MIN, ACCEL_MAX]
    self.output_a_target = 0.0
    self.output_should_stop = False
    self.shaper = AccelJerkShaper(self.dt, a_init=init_a)
    self.lead_grace = LeadGrace(self.dt)

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)

  @staticmethod
  def parse_model(model_msg):
    if (len(model_msg.position.x) == ModelConstants.IDX_N and
      len(model_msg.velocity.x) == ModelConstants.IDX_N and
      len(model_msg.acceleration.x) == ModelConstants.IDX_N):
      x = np.interp(T_IDXS_MPC, ModelConstants.T_IDXS, model_msg.position.x)
      v = np.interp(T_IDXS_MPC, ModelConstants.T_IDXS, model_msg.velocity.x)
      a = np.interp(T_IDXS_MPC, ModelConstants.T_IDXS, model_msg.acceleration.x)
      j = np.zeros(len(T_IDXS_MPC))
    else:
      x = np.zeros(len(T_IDXS_MPC))
      v = np.zeros(len(T_IDXS_MPC))
      a = np.zeros(len(T_IDXS_MPC))
      j = np.zeros(len(T_IDXS_MPC))
    if len(model_msg.meta.disengagePredictions.gasPressProbs) > 1:
      throttle_prob = model_msg.meta.disengagePredictions.gasPressProbs[1]
    else:
      throttle_prob = 1.0
    return x, v, a, j, throttle_prob

  def update(self, sm):
    # FunnyPilot v3.3.8: MPC mode selection restored (upstream semantics,
    # DEC-arbitrated). For non-mlsim model bundles (generation < 11) the MPC
    # itself runs 'blended' and tracks the model's trajectory — the v3.2.6e
    # rewrite had dropped this, leaving E2E as a pure min() clamp against the
    # ACC plan, which is why pure experimental mode would not accelerate.
    # With Dynamic Experimental Control enabled, DEC owns the acc/blended
    # decision; the fork's speed governors keep binding in BOTH modes because
    # they shape v_cruise upstream of the MPC (cruise obstacle in acc,
    # position cap in blended).
    mode = 'blended' if sm['selfdriveState'].experimentalMode else 'acc'
    if not self.mlsim:
      self.mpc.mode = mode
    LongitudinalPlannerSP.update(self, sm)
    if dec_mpc_mode := self.get_mpc_mode():
      mode = dec_mpc_mode
      if not self.mlsim:
        self.mpc.mode = dec_mpc_mode

    if len(sm['carControl'].orientationNED) == 3:
      accel_coast = get_coast_accel(sm['carControl'].orientationNED[1])
    else:
      accel_coast = ACCEL_MAX

    v_ego = sm['carState'].vEgo
    v_cruise_kph = min(sm['carState'].vCruise, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    v_cruise_initialized = sm['carState'].vCruise != V_CRUISE_UNSET

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off
    force_slow_decel = sm['controlsState'].forceDecel

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['selfdriveState'].enabled
    # PCM cruise speed may be updated a few cycles later, check if initialized
    reset_state = reset_state or not v_cruise_initialized

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    accel_clip = [ACCEL_MIN, get_max_accel(v_ego)]
    steer_angle_without_offset = sm['carState'].steeringAngleDeg - sm['liveParameters'].angleOffsetDeg
    # v3.5.4: the model's planned curvature makes the turn limit anticipatory.
    # Total (degrades to exactly the pre-v3.5.4 behaviour on any bad data).
    a_y_pred = predicted_lat_accel(sm['modelV2'].orientationRate.z,
                                   sm['modelV2'].velocity.x, v_ego)
    accel_clip = limit_accel_in_turns(v_ego, steer_angle_without_offset, accel_clip,
                                      self.CP, a_y_pred)

    if reset_state:
      self.v_desired_filter.x = v_ego
      # Clip aEgo to cruise limits to prevent large accelerations when becoming active
      self.a_desired = np.clip(sm['carState'].aEgo, accel_clip[0], accel_clip[1])
      self.shaper.reset(self.a_desired)
      self.lead_grace.reset()
      # FunnyPilot v3.5.3 — RESET THE CLIP RATE LIMITER TOO.
      # `prev_accel_clip` feeds a +/-0.05-per-frame limiter on the accel
      # CEILING (see the clip below). That limiter exists to stop the ceiling
      # stepping WHILE ENGAGED; across a disengagement there is no continuity
      # worth preserving, and leaving the stale value in place means the
      # ceiling has to walk back up at 1.0 m/s^2 per second on re-engage.
      # Symptom: disengage mid-corner (turn limiting has pulled the ceiling to
      # ~0.1) or during an SLA gas gate (which pins it to coast accel — NEGATIVE
      # on a downhill), drive manually, re-engage on a straight, and the car
      # will not accelerate for one to two seconds. Same input, different
      # response depending on invisible history, which is the definition of
      # unpredictable. This can only ever WIDEN the ceiling on the first engaged
      # frame, never narrow it, and it touches nothing while engaged.
      self.prev_accel_clip = list(accel_clip)

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))
    x, v, a, j, throttle_prob = self.parse_model(sm['modelV2'])
    # Don't clip at low speeds since throttle_prob doesn't account for creep
    self.allow_throttle = throttle_prob > ALLOW_THROTTLE_THRESHOLD or v_ego <= MIN_ALLOW_THROTTLE_SPEED

    if not self.allow_throttle:
      clipped_accel_coast = max(accel_coast, accel_clip[0])
      clipped_accel_coast_interp = np.interp(v_ego, [MIN_ALLOW_THROTTLE_SPEED, MIN_ALLOW_THROTTLE_SPEED*2], [accel_clip[1], clipped_accel_coast])
      accel_clip[1] = min(accel_clip[1], clipped_accel_coast_interp)

    # Get new v_cruise from Smart Cruise Control, Speed Limit Assist and the speed governor
    v_cruise, self.a_desired = LongitudinalPlannerSP.update_targets(self, sm, self.v_desired_filter.x, self.a_desired, v_cruise)

    # FunnyPilot v3.3.3: SLA pre-zone gas gate — approaching a lower speed
    # limit zone, clamp max accel to the measured coast accel (same mechanism
    # as allow_throttle). THROTTLE-ONLY by construction: the braking floor is
    # untouched, so lead-follow braking is unaffected and the gate itself can
    # coast the car but never brake it. The cruise target does not drop until
    # the boundary (resolver no longer early-switches), so the MPC cannot
    # brake for the new zone before entering it.
    if self.sla.gas_gate_active:
      accel_clip[1] = min(accel_clip[1], max(accel_coast, accel_clip[0]))

    if force_slow_decel:
      # Maintain 20% margin under current speed for a smooth safety decel toward a stop
      v_cruise = min(v_cruise, max(0.0, v_ego * 0.8))

    following = self.mpc.source in (LongitudinalPlanSource.lead0, LongitudinalPlanSource.lead1)

    # FunnyPilot v3.3.4: apply the hidden governor before the target reaches
    # the MPC/controls layer, unconditionally. v3.3.3st gated it off while
    # following a lead, which let the car chase a lead back up to the full
    # displayed set speed (~7% / up to ~6 mph above the governed ceiling).
    # The governed v_cruise is a ceiling, not a command: lead braking is
    # still owned entirely by the MPC's lead constraint.
    if v_cruise_initialized and not force_slow_decel and v_cruise > 0.0:
      v_cruise *= HIDDEN_CRUISE_OFFSET

    # Lead flicker/departure robustness, speed domain only (cap floored at v_ego)
    lead_one = sm['radarState'].leadOne
    v_cruise = self.lead_grace.update(bool(lead_one.status), following, lead_one.vLead, v_ego, v_cruise)

    personality = sm['selfdriveState'].personality
    self.mpc.set_weights(prev_accel_constraint, personality=personality)
    self.mpc.set_cur_state(self.v_desired_filter.x, self.a_desired)
    self.mpc.update(sm['radarState'], v_cruise, x, v, a, j, personality=personality)

    self.v_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.a_solution)
    self.j_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Interpolate 0.05 seconds and save as starting point for next iteration
    a_prev = self.a_desired
    self.a_desired = float(np.interp(self.dt, CONTROL_N_T_IDX, self.a_desired_trajectory))
    self.v_desired_filter.x = self.v_desired_filter.x + self.dt * (self.a_desired + a_prev) / 2.0

    action_t = self.CP.longitudinalActuatorDelay + DT_MDL
    output_a_target_mpc, output_should_stop_mpc = get_accel_from_plan(self.v_desired_trajectory, self.a_desired_trajectory, CONTROL_N_T_IDX,
                                                                        action_t=action_t, vEgoStopping=self.CP.vEgoStopping)
    output_a_target_e2e = sm['modelV2'].action.desiredAcceleration
    output_should_stop_e2e = sm['modelV2'].action.shouldStop

    # v3.3.8: upstream blend semantics. mlsim bundles carry e2e long in
    # action.desiredAcceleration -> min-blend when the (DEC-arbitrated) mode
    # is blended. Non-mlsim bundles don't produce a meaningful action accel
    # (the old unconditional min() against it is what froze acceleration) —
    # their e2e long IS the blended MPC solution, so take the MPC output.
    if mode == 'acc' or not self.mlsim:
      output_a_target = output_a_target_mpc
      self.output_should_stop = output_should_stop_mpc
    else:
      output_a_target = min(output_a_target_e2e, output_a_target_mpc)
      self.output_should_stop = output_should_stop_e2e or output_should_stop_mpc
      if output_a_target < output_a_target_mpc:
        self.mpc.source = LongitudinalPlanSource.e2e

    for idx in range(2):
      accel_clip[idx] = np.clip(accel_clip[idx], self.prev_accel_clip[idx] - 0.05, self.prev_accel_clip[idx] + 0.05)

    # Single comfort-shaping stage: asymmetric jerk limit, FCW bypasses
    output_a_target = self.shaper.update(output_a_target, jerk_up=get_jerk_up(personality), bypass=self.fcw)

    self.output_a_target = np.clip(output_a_target, accel_clip[0], accel_clip[1])
    self.prev_accel_clip = accel_clip

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState', 'selfdriveState', 'radarState'])

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']
    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.status
    longitudinalPlan.longitudinalPlanSource = self.mpc.source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.aTarget = float(self.output_a_target)
    longitudinalPlan.shouldStop = bool(self.output_should_stop)
    longitudinalPlan.allowBrake = True
    longitudinalPlan.allowThrottle = bool(self.allow_throttle)

    pm.send('longitudinalPlan', plan_send)

    self.publish_longitudinal_plan_sp(sm, pm)
