import math
import time
import numpy as np
from collections import deque

from cereal import log
from opendbc.car.lateral import FRICTION_THRESHOLD, get_friction
from openpilot.common.constants import ACCELERATION_DUE_TO_GRAVITY
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.controls.lib.latcontrol import LatControl
from openpilot.common.pid import PIDController

from openpilot.sunnypilot.selfdrive.controls.lib.latcontrol_torque_ext import LatControlTorqueExt

# At higher speeds (25+mph) we can assume:
# Lateral acceleration achieved by a specific car correlates to
# torque applied to the steering rack. It does not correlate to
# wheel slip, or to speed.

# This controller applies torque to achieve desired lateral
# accelerations. To compensate for the low speed effects the
# proportional gain is increased at low speeds by the PID controller.
# Additionally, there is friction in the steering wheel that needs
# to be overcome to move it at all, this is compensated for too.

KP = 0.8
KI = 0.15

INTERP_SPEEDS = [1, 1.5, 2.0, 3.0, 5, 7.5, 10, 15, 30]
KP_INTERP = [250, 120, 65, 30, 11.5, 5.5, 3.5, 2.0, KP]

LP_FILTER_CUTOFF_HZ = 1.2
JERK_LOOKAHEAD_SECONDS = 0.19
JERK_GAIN = 0.3
LAT_ACCEL_REQUEST_BUFFER_SECONDS = 1.0
VERSION = 1

# FunnyPilot v2.2.2: lane change torque state machine constants
# Torque is split into feedforward (path-tracking floor) and correction (PID error).
# Only the correction is scaled — the feedforward always runs at 100% so the car
# never applies less torque than the corner demands, preventing outside-of-turn slip.
_LC_NORMAL = 0    # full torque
_LC_CHANGING = 1  # blinker on — correction ramps 0%→100% over 5s
_LC_WAITING = 2   # blinker off, wheel not centered — correction held at 0%
_LC_TAPERING = 3  # wheel centered ≥1s — correction ramps 0%→100% over 3s
_LC_RAMP_DUR = 5.0      # seconds to ramp correction up after blinker onset
_LC_UNWIND_DEG = 15.0   # steering angle threshold to consider "centered"
_LC_CENTER_HOLD = 1.0   # seconds wheel must stay centered before tapering in
_LC_TAPER_DUR = 3.0     # seconds to ramp correction back to 100% after centering

class LatControlTorque(LatControl):
  def __init__(self, CP, CP_SP, CI, dt):
    super().__init__(CP, CP_SP, CI, dt)
    self.torque_params = CP.lateralTuning.torque.as_builder()
    self.torque_from_lateral_accel = CI.torque_from_lateral_accel()
    self.lateral_accel_from_torque = CI.lateral_accel_from_torque()
    self.pid = PIDController([INTERP_SPEEDS, KP_INTERP], KI, rate=1/self.dt)
    self.update_limits()
    self.steering_angle_deadzone_deg = self.torque_params.steeringAngleDeadzoneDeg
    self.lat_accel_request_buffer_len = int(LAT_ACCEL_REQUEST_BUFFER_SECONDS / self.dt)
    self.lat_accel_request_buffer = deque([0.] * self.lat_accel_request_buffer_len , maxlen=self.lat_accel_request_buffer_len)
    self.lookahead_frames = int(JERK_LOOKAHEAD_SECONDS / self.dt)
    self.jerk_filter = FirstOrderFilter(0.0, 1 / (2 * np.pi * LP_FILTER_CUTOFF_HZ), self.dt)

    self.extension = LatControlTorqueExt(self, CP, CP_SP, CI)

    # FunnyPilot v2.2.2: Lane change torque state machine
    # Correction (PID error) is scaled; feedforward (path-tracking) always runs at 100%.
    # NORMAL → CHANGING (blinker) → WAITING (blinker off, wheel off-center)
    #   → TAPERING (wheel ±15° for ≥1s, correction 0%→100% over 3s) → NORMAL
    self._lc_state = _LC_NORMAL
    self._lc_start = 0.0
    self._lc_taper_start = 0.0
    self._lc_centered_since = 0.0
    self._prev_blinker_on = False

  def update_live_torque_params(self, latAccelFactor, latAccelOffset, friction):
    self.torque_params.latAccelFactor = latAccelFactor
    self.torque_params.latAccelOffset = latAccelOffset
    self.torque_params.friction = friction
    self.update_limits()

  def update_limits(self):
    self.pid.set_limits(self.lateral_accel_from_torque(self.steer_max, self.torque_params),
                        self.lateral_accel_from_torque(-self.steer_max, self.torque_params))

  def update(self, active, CS, VM, params, steer_limited_by_safety, desired_curvature, calibrated_pose, curvature_limited, lat_delay):
    # Override torque params from extension
    if self.extension.update_override_torque_params(self.torque_params):
      self.update_limits()

    pid_log = log.ControlsState.LateralTorqueState.new_message()
    pid_log.version = VERSION
    measured_curvature = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
    measurement = measured_curvature * CS.vEgo ** 2
    future_desired_lateral_accel = desired_curvature * CS.vEgo ** 2
    self.lat_accel_request_buffer.append(future_desired_lateral_accel)

    roll_compensation = params.roll * ACCELERATION_DUE_TO_GRAVITY
    curvature_deadzone = abs(VM.calc_curvature(math.radians(self.steering_angle_deadzone_deg), CS.vEgo, 0.0))
    lateral_accel_deadzone = curvature_deadzone * CS.vEgo ** 2

    delay_frames = int(np.clip(lat_delay / self.dt + 1, 1, self.lat_accel_request_buffer_len))
    expected_lateral_accel = self.lat_accel_request_buffer[-delay_frames]
    setpoint = expected_lateral_accel
    error = setpoint - measurement

    lookahead_idx = int(np.clip(-delay_frames + self.lookahead_frames, -self.lat_accel_request_buffer_len+1, -2))
    raw_lateral_jerk = (self.lat_accel_request_buffer[lookahead_idx+1] - self.lat_accel_request_buffer[lookahead_idx-1]) / (2 * self.dt)
    desired_lateral_jerk = self.jerk_filter.update(raw_lateral_jerk)
    gravity_adjusted_future_lateral_accel = future_desired_lateral_accel - roll_compensation
    ff = gravity_adjusted_future_lateral_accel
    # latAccelOffset corrects roll compensation bias from device roll misalignment relative to car roll
    ff -= self.torque_params.latAccelOffset
    ff += get_friction(error + JERK_GAIN * desired_lateral_jerk, lateral_accel_deadzone, FRICTION_THRESHOLD, self.torque_params)

    if not active:
      output_torque = 0.0
      pid_log.active = False
    else:
      # do error correction in lateral acceleration space, convert at end to handle non-linear torque responses correctly
      pid_log.error = float(error)

      freeze_integrator = steer_limited_by_safety or CS.steeringPressed or CS.vEgo < 5
      output_lataccel = self.pid.update(pid_log.error, speed=CS.vEgo, feedforward=ff, freeze_integrator=freeze_integrator)
      output_torque = self.torque_from_lateral_accel(output_lataccel, self.torque_params)

      # Lateral acceleration torque controller extension updates
      # Overrides pid_log.error and output_torque
      pid_log, output_torque = self.extension.update(CS, VM, self.pid, params, ff, pid_log, setpoint, measurement, calibrated_pose, roll_compensation,
                                                     future_desired_lateral_accel, measurement, lateral_accel_deadzone, gravity_adjusted_future_lateral_accel,
                                                     desired_curvature, measured_curvature, steer_limited_by_safety, output_torque)

      # FunnyPilot v2.2.2: Lane change torque state machine
      # Feedforward torque (path/corner tracking) always runs at 100%.
      # Only the PID correction is scaled so the car never applies less
      # torque than the curve demands — prevents outside-of-turn slip.
      blinker_on = CS.leftBlinker != CS.rightBlinker
      steer_abs = abs(CS.steeringAngleDeg)
      now = time.monotonic()

      # State transitions
      if self._lc_state == _LC_NORMAL:
        if blinker_on and not self._prev_blinker_on:
          self._lc_state = _LC_CHANGING
          self._lc_start = now
          self._lc_centered_since = 0.0
      elif self._lc_state == _LC_CHANGING:
        if not blinker_on:
          self._lc_centered_since = now if steer_abs < _LC_UNWIND_DEG else 0.0
          self._lc_state = _LC_WAITING
      elif self._lc_state == _LC_WAITING:
        if blinker_on and not self._prev_blinker_on:
          self._lc_state = _LC_CHANGING
          self._lc_start = now
          self._lc_centered_since = 0.0
        elif steer_abs < _LC_UNWIND_DEG:
          if self._lc_centered_since == 0.0:
            self._lc_centered_since = now
          elif now - self._lc_centered_since >= _LC_CENTER_HOLD:
            self._lc_state = _LC_TAPERING
            self._lc_taper_start = now
        else:
          self._lc_centered_since = 0.0  # went outside window; reset hold timer
      elif self._lc_state == _LC_TAPERING:
        if now - self._lc_taper_start >= _LC_TAPER_DUR:
          self._lc_state = _LC_NORMAL
        elif blinker_on and not self._prev_blinker_on:
          self._lc_state = _LC_CHANGING
          self._lc_start = now
          self._lc_centered_since = 0.0

      # Compute correction scale (feedforward always at 1.0)
      if self._lc_state == _LC_NORMAL:
        correction_scale = 1.0
      elif self._lc_state == _LC_CHANGING:
        correction_scale = min(1.0, (now - self._lc_start) / _LC_RAMP_DUR)
      elif self._lc_state == _LC_WAITING:
        correction_scale = 0.0
      else:  # _LC_TAPERING
        correction_scale = min(1.0, (now - self._lc_taper_start) / _LC_TAPER_DUR)

      # Split torque into feedforward floor + scalable correction.
      # ff_torque = minimum torque needed to track the current path (corner).
      # Correction = everything on top of that (PID error response).
      ff_torque = self.torque_from_lateral_accel(ff, self.torque_params)
      correction = output_torque - ff_torque
      output_torque = ff_torque + correction * correction_scale
      self._prev_blinker_on = blinker_on

      # FunnyPilot v2.2.0: Smooth stop — linear torque ramp from 100% at 10mph to 0% at 0mph
      speed_mph = CS.vEgo * 2.23694
      if speed_mph < 10.0:
        output_torque *= max(0.0, speed_mph / 10.0)

      pid_log.active = True
      pid_log.p = float(self.pid.p)
      pid_log.i = float(self.pid.i)
      pid_log.d = float(self.pid.d)
      pid_log.f = float(self.pid.f)
      pid_log.output = float(-output_torque) # TODO: log lat accel?
      pid_log.actualLateralAccel = float(measurement)
      pid_log.desiredLateralAccel = float(setpoint)
      pid_log.desiredLateralJerk = float(desired_lateral_jerk)
      pid_log.saturated = bool(self._check_saturation(self.steer_max - abs(output_torque) < 1e-3, CS, steer_limited_by_safety, curvature_limited))

    # TODO left is positive in this convention
    return -output_torque, 0.0, pid_log
