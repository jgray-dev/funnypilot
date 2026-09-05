import numpy as np
from cereal import car
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N
from openpilot.common.pid import PIDController
from openpilot.selfdrive.modeld.constants import ModelConstants

CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]

# FunnyPilot v3.2.6e: slew rate for the starting state, m/s^3. Replaces the
# instantaneous step to CP.startAccel — reaches a typical 1.2-1.6 m/s^2 launch
# accel in ~0.2-0.3 s, fast enough to release brake-hold on cars that need the
# kick, without the head-snap of a step command.
STARTING_ACCEL_RATE = 6.0

# FunnyPilot v3.5.4 — the starting ramp is now SCHEDULED ON THE ACCEL ITSELF,
# not constant. The ramp does two completely different jobs back to back:
#
#   while output is NEGATIVE it is RELEASING THE BRAKE. That must stay brisk;
#   slowing it is a car that sits at a green light.
#   once output is POSITIVE it is APPLYING LAUNCH TORQUE. That is where the
#   head-snap lives, and it is the half worth softening.
#
# Interpolating on the current accel rather than switching at zero keeps the
# rate itself continuous, so there is no kink where the two jobs meet.
STARTING_CREEP_RATE = 2.5      # m/s^3, once actually pulling away
STARTING_RATE_BP = [-0.5, 0.5]  # m/s^2 of current output accel

# FunnyPilot v3.5.5 — THE v3.5.4 STOPPING TAPER IS REVERTED. POST-MORTEM, and
# it is the useful part of this file:
#
# v3.5.4 scaled `CP.stoppingDecelRate` down to 0.35x while the car was still
# rolling, to soften the last bite of brake at the end of a stop. The stated
# safety argument was "this cannot stop the car later than commanded, because
# the ramp starts from last_output_accel and only adds more on top". That
# sentence is true and IRRELEVANT, which is the trap worth remembering: in the
# `stopping` state the PID IS RESET AND PRODUCES NOTHING, so this ramp is the
# ONLY brake authority the car has. Slowing it does not merely soften an extra
# bite — it slows the entire completion of the stop.
#
# The numbers were never checked against THIS car, and that is what made it a
# defect rather than a taste call. Upstream's default `stoppingDecelRate` is
# 0.8 m/s^3, but the K5 takes sunnypilot's Hyundai DEFAULT config, which is
# 0.40 (opendbc/sunnypilot/car/hyundai/longitudinal/config.py). Walking from
# 0 to `stopAccel` -2.0 therefore takes 5 s at full rate — and 14 s at 0.35x.
# The stopping state became very nearly a HOLD of whatever accel happened to be
# commanded when it engaged, with the deficit paid back at full rate only once
# under 0.5 m/s. Softer, then a grab: exactly the "too hard, too late" the
# driver reported, at the one moment it is most obvious.
#
# GENERALIZED: a rate limiter is only "just a comfort scale" when something
# else owns the target. Check what else is driving before you slow one down.


def starting_accel_rate(a_now: float) -> float:
  """Slew rate for the starting state, m/s^3. See STARTING_RATE_BP."""
  return float(np.interp(a_now, STARTING_RATE_BP, [STARTING_ACCEL_RATE, STARTING_CREEP_RATE]))

LongCtrlState = car.CarControl.Actuators.LongControlState


def long_control_state_trans(CP, CP_SP, active, long_control_state, v_ego,
                             should_stop, brake_pressed, cruise_standstill):
  # Gas Interceptor
  cruise_standstill = cruise_standstill and not CP_SP.enableGasInterceptor

  stopping_condition = should_stop
  starting_condition = (not should_stop and
                        not cruise_standstill and
                        not brake_pressed)
  started_condition = v_ego > CP.vEgoStarting

  if not active:
    long_control_state = LongCtrlState.off

  else:
    if long_control_state == LongCtrlState.off:
      if not starting_condition:
        long_control_state = LongCtrlState.stopping
      else:
        if starting_condition and CP.startingState:
          long_control_state = LongCtrlState.starting
        else:
          long_control_state = LongCtrlState.pid

    elif long_control_state == LongCtrlState.stopping:
      if starting_condition and CP.startingState:
        long_control_state = LongCtrlState.starting
      elif starting_condition:
        long_control_state = LongCtrlState.pid

    elif long_control_state in [LongCtrlState.starting, LongCtrlState.pid]:
      if stopping_condition:
        long_control_state = LongCtrlState.stopping
      elif started_condition:
        long_control_state = LongCtrlState.pid
  return long_control_state

class LongControl:
  def __init__(self, CP, CP_SP):
    self.CP = CP
    self.CP_SP = CP_SP
    self.long_control_state = LongCtrlState.off
    self.pid = PIDController((CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV),
                             (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV),
                             rate=1 / DT_CTRL)
    self.last_output_accel = 0.0

  def reset(self):
    self.pid.reset()

  def update(self, active, CS, a_target, should_stop, accel_limits):
    """Update longitudinal control. This updates the state machine and runs a PID loop"""
    self.pid.neg_limit = accel_limits[0]
    self.pid.pos_limit = accel_limits[1]

    prev_state = self.long_control_state
    self.long_control_state = long_control_state_trans(self.CP, self.CP_SP, active, self.long_control_state, CS.vEgo,
                                                       should_stop, CS.brakePressed,
                                                       CS.cruiseState.standstill)
    if self.long_control_state == LongCtrlState.off:
      self.reset()
      output_accel = 0.

    elif self.long_control_state == LongCtrlState.stopping:
      # v3.7.1a: stop hold may add braking, but must not discard a stronger
      # planner request. The PID is inactive here; ignoring a_target could
      # turn a -3 m/s^2 demand into just -0.004 on the first stopping frame.
      output_accel = min(self.last_output_accel, a_target, 0.0)
      if output_accel > self.CP.stopAccel:
        output_accel = min(output_accel, 0.0)
        # v3.5.5: full rate again — this ramp is the ONLY brake authority in
        # this state (the PID is reset below), so it may not be scaled.
        output_accel -= self.CP.stoppingDecelRate * DT_CTRL
      self.reset()

    elif self.long_control_state == LongCtrlState.starting:
      # Jerk-limited ramp toward startAccel instead of a step. v3.5.4 schedules
      # the rate on the accel itself: brisk while releasing the brake, gentle
      # once actually pulling away.
      rate = starting_accel_rate(self.last_output_accel)
      output_accel = min(self.last_output_accel + rate * DT_CTRL, self.CP.startAccel)
      self.reset()

    else:  # LongCtrlState.pid
      error = a_target - CS.aEgo
      if prev_state in (LongCtrlState.stopping, LongCtrlState.starting):
        # Bumpless transfer: seed the integrator so the first PID output
        # continues from the last commanded accel instead of stepping to
        # feedforward + P from a zeroed controller.
        self.pid.speed = CS.vEgo
        self.pid.i = float(np.clip(self.last_output_accel - a_target - self.pid.k_p * error,
                                   accel_limits[0], accel_limits[1]))
      output_accel = self.pid.update(error, speed=CS.vEgo,
                                     feedforward=a_target)

    self.last_output_accel = np.clip(output_accel, accel_limits[0], accel_limits[1])
    return self.last_output_accel
