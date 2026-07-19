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
import math
import numpy as np

import cereal.messaging as messaging
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from cereal import log, custom
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import CRUISE_MIN_ACCEL, STOP_DISTANCE
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, get_accel_from_plan
from openpilot.selfdrive.controls.lib.long_shaping import AccelJerkShaper, LeadGrace, lead_urgency
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
# FunnyPilot v3.3.7: cruise-envelope deceleration authority by context. The
# MPC's fake cruise obstacle assumes CRUISE_MIN_ACCEL (-1.2) — enough for set
# speed changes, too weak when a governed cap needs real braking. Verified
# tight corners (SCC-M and SCC-V agreeing) get -2.0; a single curve governor
# or an e2e model stop gets -1.6. These are BOUNDS on the planning envelope,
# not commands — the MPC still plans the smoothest trajectory that meets the
# cap, so ordinary decels are unchanged.
CRUISE_MIN_ACCEL_CURVE = -1.6
CRUISE_MIN_ACCEL_CURVE_VERIFIED = -2.0
CRUISE_MIN_ACCEL_E2E_STOP = -1.6

# FunnyPilot v3.3.7: e2e stop assist. In e2e/blended mode the model's own
# velocity plan encodes intended stops (signs/lights), but only its late
# action.desiredAcceleration was consumed — the car arrived at intersections
# 10-15 mph hot. Convert the plan into an allowed-now speed cap with a
# constant-decel budget (same math as the curve governors):
#   v_cap = min_i(v_plan_i + E2E_STOP_DECEL * t_i)
# A plan that stays at speed gives no constraint; a planned stop caps the
# cruise target early, so the MPC bleeds speed BEFORE the model's action
# decel arrives, which then only has to finish the job.
E2E_STOP_DECEL = 1.2  # m/s^2 budget mapping the model's future speeds to allowed-now
E2E_STOP_MIN_V = 2.0  # m/s — below this leave creep/stopping to the action path

JERK_UP_AGGRESSIVE = 2.5
JERK_UP_STANDARD = 1.8
JERK_UP_RELAXED = 1.4

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]

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

def limit_accel_in_turns(v_ego, angle_steers, a_target, CP):
  """
  This function returns a limited long acceleration allowed, depending on the existing lateral acceleration
  this should avoid accelerating when losing the target in turns
  """
  # FIXME: This function to calculate lateral accel is incorrect and should use the VehicleModel
  # The lookup table for turns should also be updated if we do this
  a_total_max = np.interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
  a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))

  return [a_target[0], min(a_target[1], a_x_allowed)]


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
    LongitudinalPlannerSP.update(self, sm)

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
    accel_clip = limit_accel_in_turns(v_ego, steer_angle_without_offset, accel_clip, self.CP)

    if reset_state:
      self.v_desired_filter.x = v_ego
      # Clip aEgo to cruise limits to prevent large accelerations when becoming active
      self.a_desired = np.clip(sm['carState'].aEgo, accel_clip[0], accel_clip[1])
      self.shaper.reset(self.a_desired)
      self.lead_grace.reset()

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))
    _, v_plan_model, _, _, throttle_prob = self.parse_model(sm['modelV2'])
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

    # FunnyPilot v3.3.7: braking authority for the governed cap, by context.
    # NOTE: self.source (LongitudinalPlannerSP) is the CUSTOM plan-source enum,
    # not the stock log one the MPC uses.
    _sp_source = custom.LongitudinalPlanSP.LongitudinalPlanSource
    cruise_min_accel = CRUISE_MIN_ACCEL
    if self.source in (_sp_source.sccMap, _sp_source.sccVision):
      both_curves = self._scc_map_v2.is_active and self._scc_vision_v2.is_active
      cruise_min_accel = CRUISE_MIN_ACCEL_CURVE_VERIFIED if both_curves else CRUISE_MIN_ACCEL_CURVE

    # FunnyPilot v3.3.7: e2e stop assist — the model's own velocity plan as a
    # speed cap (see constants above). Only in e2e/blended mode, where the
    # plan genuinely encodes stops; the min with v_cruise means it can only
    # slow us, and the action-decel min-pick below is unchanged.
    if self.is_e2e(sm) and v_ego > E2E_STOP_MIN_V and len(v_plan_model) == len(T_IDXS_MPC):
      v_plan_cap = float(np.min(np.maximum(v_plan_model, 0.0) + E2E_STOP_DECEL * np.asarray(T_IDXS_MPC)))
      if np.isfinite(v_plan_cap) and v_plan_cap < v_cruise:
        v_cruise = max(v_plan_cap, 0.0)
        cruise_min_accel = min(cruise_min_accel, CRUISE_MIN_ACCEL_E2E_STOP)

    # Lead flicker/departure robustness, speed domain only (cap floored at v_ego)
    lead_one = sm['radarState'].leadOne
    v_cruise = self.lead_grace.update(bool(lead_one.status), following, lead_one.vLead, v_ego, v_cruise)

    # FunnyPilot v3.3.7: lead-approach urgency (see long_shaping.lead_urgency).
    # 0 in all ordinary following/decels; rises only when the deceleration
    # REQUIRED to stop behind the lead approaches the planning envelope's own
    # COMFORT_BRAKE — the "closing fast on a stopped car" case that previously
    # under-braked into a manual takeover.
    urgency = lead_urgency(lead_one.dRel, v_ego, lead_one.vLead, STOP_DISTANCE) if lead_one.status else 0.0

    personality = sm['selfdriveState'].personality
    self.mpc.set_weights(prev_accel_constraint, personality=personality, urgency=urgency)
    self.mpc.set_cur_state(self.v_desired_filter.x, self.a_desired)
    self.mpc.update(sm['radarState'], v_cruise, personality=personality, cruise_min_accel=cruise_min_accel)

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

    if self.is_e2e(sm):
      output_a_target = min(output_a_target_e2e, output_a_target_mpc)
      self.output_should_stop = output_should_stop_e2e or output_should_stop_mpc
      if output_a_target < output_a_target_mpc:
        self.mpc.source = LongitudinalPlanSource.e2e
    else:
      output_a_target = output_a_target_mpc
      self.output_should_stop = output_should_stop_mpc

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
