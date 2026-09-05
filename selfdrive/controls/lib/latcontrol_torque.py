import math
import time
import numpy as np
from collections import deque

from cereal import log
from opendbc.car.lateral import FRICTION_THRESHOLD, get_friction
from openpilot.common.constants import ACCELERATION_DUE_TO_GRAVITY
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.controls.lib.latcontrol import LatControl
from openpilot.selfdrive.controls.lib.lat_handback import LatHandback, PRESS_SCALE
from openpilot.selfdrive.controls.lib.eps_limit import EpsTorqueGovernor
from openpilot.selfdrive.controls.lib.bump_damper import BumpDamper
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

# FunnyPilot v3.4.9: time constant for bleeding the integrator while the driver
# is holding the wheel. See the handback block in update().
INTEGRATOR_BLEED_TAU = 1.0  # s

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

    # FunnyPilot v3.0.9e: soft lane change — scale TOTAL steering torque
    # (feedforward + correction) so the whole maneuver eases in, not just the
    # error correction. A floor keeps enough authority that corners aren't lost.
    # On blinker ON:  total ramps 45% → 100% over 6 s.
    # On blinker OFF: hold 45% for 0.5 s, then 45% → 100% over 2 s (alpha² ease-in).
    self._LC_MIN_SCALE    = 0.45  # total-torque floor during lane change
    self._LC_RAMP_DUR     = 6.0   # blinker-on ramp duration (s)
    self._POST_DELAY      = 0.5   # hold-at-floor after blinker off (s)
    self._POST_RAMP_DUR   = 2.0   # ramp back to full after the hold (s)
    self.lane_change_torque_scale = 1.0
    self._lc_blinker_on_time  = -1e9  # sentinel: blinker not recently ON
    self._lc_blinker_off_time = -1e9  # sentinel: blinker not recently OFF
    self._prev_blinker_on = False

    # FunnyPilot v3.2.1e: blinker-unwind re-engage ramp. When the blinker-pause
    # feature releases lateral control (active False->True after a blinker), ramp
    # TOTAL torque CONTINUOUSLY from _REENGAGE_RAMP_START -> 100% over
    # _REENGAGE_RAMP_DUR. The scale is recomputed every 100 Hz control frame as a
    # smooth linear function of elapsed time (not stepped quarters) so authority
    # returns gradually instead of snapping to full torque mid-curve. Scoped to
    # blinker pauses (a blinker seen while inactive) — a plain engage or standstill
    # release gets instant authority.
    # v3.2.1st: more aggressive re-engage — start at 15% (not 0%) and reach 100%
    # over 3 s (was 0% over 4 s) so the wheel takes hold a bit sooner.
    self._REENGAGE_RAMP_START   = 0.15  # initial torque fraction at re-engage
    self._REENGAGE_RAMP_DUR     = 3.0
    self._prev_active           = False
    self._inactive_saw_blinker  = False
    self._reengage_start_time   = -1e9

    # FunnyPilot v3.2.3st: driver-override softening. The v3.2.2 interpolation
    # sends firmer, more consistent curvature commands, so manually pushing the
    # wheel away now meets more resistance. When the driver is actively applying
    # torque we scale the TOTAL output torque down to _OVERRIDE_MIN_SCALE so it
    # takes less force to retake the wheel. Panda hardware torque limits remain
    # the safety backstop; exact no-op (scale 1.0) with no intervention.
    # v3.2.8: the raw CS.steeringPressed trigger caused a bite-then-loosen limit
    # cycle — wheel-inertia reaction torque during hard bites can latch
    # steeringPressed with no driver involved, cutting torque, which released the
    # bar, which restored torque, at a few Hz. The trigger is gated by
    # OverrideGate (see override_gate.py): it engages only after a SUSTAINED
    # press (0.4 s) and releases only after a sustained let-go (0.3 s), so it can
    # never alternate against the controller's own output.
    # v3.4.9: OverrideGate fixed the CHATTER but nothing scheduled the RETURN —
    # the release was a 0.15 s first-order step, identical whether the model was
    # 0.1 or 3 m/s^2 away from what the driver had just established. That step
    # is the reported "as soon as it takes control it jumps too far right",
    # once per corner. LatHandback (lat_handback.py) now owns the whole scale:
    # same floor on the way in, and a return RAMP whose duration is scheduled
    # by the desired-vs-actual lateral-accel divergence — 1.6 s when the two
    # are close (nothing to correct, so no reason to snatch), 0.45 s when they
    # are far apart (an evasive move genuinely needs authority back).
    self._OVERRIDE_MIN_SCALE  = PRESS_SCALE  # total-torque floor while the driver presses
    self._override_scale      = 1.0
    self._handback            = LatHandback(self.dt)

    # FunnyPilot v3.3.8: EPS torque governor — mirror the K5's hardware
    # driver-torque clamp + slew limits inside the controller (see
    # eps_limit.py for the grab-then-loosen mechanism). Applied LAST, so the
    # request that leaves latcontrol is always realizable by the rack; its
    # damped post-clamp recovery is what breaks the turn-in torque
    # oscillation. Only ever reduces torque; panda remains the backstop.
    self._eps_governor = EpsTorqueGovernor(self.dt)

    # v3.3.8: bump/weight-transfer damper (bump_damper.py) — ACTED-ON
    # hypothesis for the railroad-track oscillation. Softens the error
    # channel through a detected pitch-rate spike so a bump-induced
    # measurement/setpoint disturbance can't ring through the high-gain
    # friction relay. Only ever reduces the correction toward pure
    # feedforward; never adds torque.
    self._bump_damper = BumpDamper(self.dt)

  def update_live_torque_params(self, latAccelFactor, latAccelOffset, friction):
    self.torque_params.latAccelFactor = 2.750  # FunnyPilot: locked LAF
    self.torque_params.latAccelOffset = latAccelOffset
    self.torque_params.friction = friction
    self.update_limits()

  def update_limits(self):
    self.pid.set_limits(self.lateral_accel_from_torque(self.steer_max, self.torque_params),
                        self.lateral_accel_from_torque(-self.steer_max, self.torque_params))

  def reset(self):
    super().reset()
    self.pid.reset()
    self.extension.reset()

  def update(self, active, CS, VM, params, steer_limited_by_safety, desired_curvature, calibrated_pose, curvature_limited, lat_delay):
    # Override torque params from extension
    if self.extension.update_override_torque_params(self.torque_params):
      self.update_limits()
    neural = self.extension.prepare_pid(self.pid)

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
    setpoint = self.lat_accel_request_buffer[-delay_frames]
    error = setpoint - measurement
    raw_measurement = measurement  # kept for honest pid_log; error/measurement below may be damped

    # v3.3.8: bump/weight-transfer damper. On a detected pitch-rate spike,
    # blend measurement TOWARD setpoint (shrinking |error|, never holding it —
    # holding under a ramping setpoint would manufacture GROWING error and
    # thus MORE torque, exactly backwards). See bump_damper.py for the full
    # mechanism writeup. Exact no-op (damp==1.0) outside a detected window.
    pitch_rate_deg = math.degrees(calibrated_pose.angular_velocity.pitch) if calibrated_pose is not None else 0.0
    damp = self._bump_damper.update(pitch_rate_deg)
    if damp < 1.0:
      measurement = setpoint + damp * (measurement - setpoint)
      error = setpoint - measurement

    lookahead_idx = int(np.clip(-delay_frames + self.lookahead_frames, -self.lat_accel_request_buffer_len+1, -2))
    raw_lateral_jerk = (self.lat_accel_request_buffer[lookahead_idx+1] - self.lat_accel_request_buffer[lookahead_idx-1]) / (2 * self.dt)
    desired_lateral_jerk = self.jerk_filter.update(raw_lateral_jerk)
    gravity_adjusted_future_lateral_accel = future_desired_lateral_accel - roll_compensation
    ff = gravity_adjusted_future_lateral_accel
    # latAccelOffset corrects roll compensation bias from device roll misalignment relative to car roll
    ff -= self.torque_params.latAccelOffset
    # v3.3.8: the jerk-lookahead term also replays the delay buffer, so its
    # contribution to the friction relay is damped by the same factor.
    ff += get_friction(error + damp * JERK_GAIN * desired_lateral_jerk, lateral_accel_deadzone, FRICTION_THRESHOLD, self.torque_params)

    # FunnyPilot v3.2.1e: track active edges + whether a blinker was involved
    # while inactive, to drive the blinker-unwind re-engage torque ramp below.
    now = time.monotonic()
    one_blinker = CS.leftBlinker != CS.rightBlinker  # exactly one blinker on
    if not active and one_blinker:
      self._inactive_saw_blinker = True
    if active and not self._prev_active:
      # rising edge: ramp only if this re-engage followed a blinker pause
      self._reengage_start_time = now if self._inactive_saw_blinker else -1e9
      self._inactive_saw_blinker = False
    self._prev_active = active

    if not active:
      self.reset()
      output_torque = 0.0
      pid_log.active = False
      # keep the driver-override softening reset so a re-engage starts at full scale
      self._handback.reset()
      self._override_scale = 1.0
      self._eps_governor.reset()
      self._bump_damper.reset()
    else:
      # do error correction in lateral acceleration space, convert at end to handle non-linear torque responses correctly
      pid_log.error = float(error)

      # v3.4.9: the handback schedule is computed BEFORE the PID runs, because
      # it also decides what the integrator is allowed to do. The divergence it
      # measures is the delayed desired lat accel against the RAW measurement —
      # the same two numbers the error is built from, undamped, so the schedule
      # reflects the road and not the bump damper.
      self._override_scale = self._handback.update(CS.steeringPressed, setpoint, raw_measurement)

      # v3.4.9: while the driver is actually in charge, BLEED the integrator
      # rather than merely freezing it. A frozen integrator still holds
      # whatever it wound up to before the intervention (mid-corner: a demand
      # for more turn), and dumping that back in at handback is the other half
      # of the reported bite. The bleed is slow enough (~1 s time constant)
      # that a brief inertia blip costs nothing.
      if self._handback.engaged:
        self.pid.i *= (1.0 - self.dt / INTEGRATOR_BLEED_TAU)

      # v3.3.8: also freeze while the EPS governor's driver-limit bound is
      # clamping (previous frame's state) — the hardware won't take more
      # torque, so integrating the tracking error would only wind up an
      # overshoot for when authority returns.
      # v3.4.9: and through the first half of the handback ramp, where the
      # error is still the driver's own doing.
      freeze_integrator = (steer_limited_by_safety or CS.steeringPressed or CS.vEgo < 5 or
                          self._eps_governor.driver_limited or self._bump_damper.active or
                          self._handback.soft_integrator)
      output_torque = 0.0
      if not neural:
        output_lataccel = self.pid.update(pid_log.error, speed=CS.vEgo, feedforward=ff, freeze_integrator=freeze_integrator)
        output_torque = self.torque_from_lateral_accel(output_lataccel, self.torque_params)

      # Lateral acceleration torque controller extension updates
      # Overrides pid_log.error and output_torque
      pid_log, output_torque = self.extension.update(CS, VM, self.pid, params, ff, pid_log, setpoint, measurement, calibrated_pose, roll_compensation,
                                                     future_desired_lateral_accel, measurement, lateral_accel_deadzone, gravity_adjusted_future_lateral_accel,
                                                     desired_curvature, measured_curvature, steer_limited_by_safety, output_torque,
                                                     freeze_integrator=freeze_integrator)

      # FunnyPilot v3.0.9e: soft lane change — scale the TOTAL steering torque
      # (feedforward + correction together) so the whole maneuver eases in. A floor
      # (_LC_MIN_SCALE) keeps enough authority that we don't lose a corner mid-change.
      # Blinker ON:  floor → 100% over _LC_RAMP_DUR.
      # Blinker OFF: hold at floor for _POST_DELAY, then floor → 100% over
      #              _POST_RAMP_DUR (alpha² ease-in) to settle gently into the new lane.
      blinker_on = one_blinker
      if blinker_on and not self._prev_blinker_on:
        self._lc_blinker_on_time  = now
        self._lc_blinker_off_time = -1e9  # cancel any in-progress post-ramp
      if not blinker_on and self._prev_blinker_on:
        self._lc_blinker_off_time = now
      self._prev_blinker_on = blinker_on

      floor = self._LC_MIN_SCALE
      if blinker_on:
        ramp = min((now - self._lc_blinker_on_time) / self._LC_RAMP_DUR, 1.0)
        scale = floor + (1.0 - floor) * ramp
      else:
        post = now - self._lc_blinker_off_time
        if post < self._POST_DELAY:
          scale = floor
        elif post < self._POST_DELAY + self._POST_RAMP_DUR:
          alpha = (post - self._POST_DELAY) / self._POST_RAMP_DUR
          scale = floor + (1.0 - floor) * alpha * alpha  # ease-in back to full
        else:
          scale = 1.0
      self.lane_change_torque_scale = scale

      output_torque *= scale

      # FunnyPilot v3.2.1e: blinker-unwind re-engage ramp — continuous linear,
      # evaluated fresh each frame. v3.2.1st: 15% -> 100% over 3 s. Still a no-op
      # (scale 1.0) for non-blinker engages, where _reengage_start_time stays at its
      # -1e9 sentinel: the progress term saturates to 1.0, so the scale resolves to
      # _REENGAGE_RAMP_START + (1 - _REENGAGE_RAMP_START) = 1.0.
      reengage_progress = min(max((now - self._reengage_start_time) / self._REENGAGE_RAMP_DUR, 0.0), 1.0)
      reengage_scale = self._REENGAGE_RAMP_START + (1.0 - self._REENGAGE_RAMP_START) * reengage_progress
      output_torque *= reengage_scale

      # FunnyPilot: Smooth stopping - Reduce torque linearly from 0-15mph
      speed_mph = CS.vEgo * 2.23694
      if speed_mph < 15.0:
        torque_scale = max(0.0, speed_mph / 15.0)
        output_torque *= torque_scale

      # FunnyPilot v3.2.3st driver-override softening, v3.2.8 gated, v3.4.9
      # divergence-scheduled on the way back out. The scale was computed at the
      # top of this branch (it also gates the integrator); apply it here, in
      # the same place in the chain it has always been applied.
      output_torque *= self._override_scale

      # FunnyPilot v3.3.8: EPS torque governor, LAST in the chain — clamp the
      # request to what the carcontroller/panda driver-torque limit will
      # actually pass (with damped recovery) and to the rack's +3/-7 slew.
      # Runs in the ACTUATOR frame (this function returns -output_torque), the
      # same frame as CS.steeringTorque.
      output_torque = -self._eps_governor.update(-output_torque, CS.steeringTorque)

      pid_log.active = True
      pid_log.p = float(self.pid.p)
      pid_log.i = float(self.pid.i)
      pid_log.d = float(self.pid.d)
      pid_log.f = float(self.pid.f)
      pid_log.output = float(-output_torque) # TODO: log lat accel?
      pid_log.actualLateralAccel = float(raw_measurement)  # log the RAW measurement, not the damped one
      pid_log.desiredLateralAccel = float(setpoint)
      pid_log.desiredLateralJerk = float(desired_lateral_jerk)
      # v3.3.8: a sustained EPS-governor clamp is real authority loss in a
      # corner — it must feed the saturation alert exactly like hitting
      # steer_max (the _check_saturation dwell filters out transients).
      saturated_now = self.steer_max - abs(output_torque) < 1e-3 or self._eps_governor.driver_limited
      pid_log.saturated = bool(self._check_saturation(saturated_now, CS, steer_limited_by_safety, curvature_limited))

    # TODO left is positive in this convention
    return -output_torque, 0.0, pid_log
