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

# The stopping ramp's rate is likewise tapered WHILE STILL ROLLING. It walks
# accel down toward CP.stopAccel after the plan has decided to stop; at full
# rate that last bite of brake pressure arrives while the car is still moving
# and is exactly the nod you feel at the end of a stop. Below STOP_TAPER_V the
# full rate is restored so the brake hold is always secured firmly.
# THIS CANNOT MAKE THE CAR STOP LATER THAN COMMANDED: the ramp starts from
# `last_output_accel`, which is already whatever the planner asked for, and
# only ever adds MORE braking on top.
STOP_TAPER_V = [0.5, 1.5]      # m/s
STOP_TAPER_SCALE = [1.0, 0.35]  # fraction of CP.stoppingDecelRate


def starting_accel_rate(a_now: float) -> float:
  """Slew rate for the starting state, m/s^3. See STARTING_RATE_BP."""
  return float(np.interp(a_now, STARTING_RATE_BP, [STARTING_ACCEL_RATE, STARTING_CREEP_RATE]))


def stopping_decel_rate(base_rate: float, v_ego: float) -> float:
  """Slew rate for the stopping state, m/s^3. See STOP_TAPER_V."""
  return float(base_rate * np.interp(v_ego, STOP_TAPER_V, STOP_TAPER_SCALE))

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
      output_accel = self.last_output_accel
      if output_accel > self.CP.stopAccel:
        output_accel = min(output_accel, 0.0)
        # v3.5.4: tapered while still rolling, full rate once nearly stopped
        output_accel -= stopping_decel_rate(self.CP.stoppingDecelRate, CS.vEgo) * DT_CTRL
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
