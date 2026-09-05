"""Use wheel travel already in progress to settle a steering correction.

Curvature interpolation cannot prevent the torque loop from pushing through
a target the wheel is already approaching. Compare measured wheel travel over
a short preview with the reference travel ALREADY in the actuator-delay
buffer. Only surplus travel into the tracking error earns motion credit.

This changes error feedback, not the path or its feedforward. A stationary
wheel, growing error, or a wheel following a planned ramp earns no credit.
There is no pending curvature/torque debt to deliver later.
"""
import math
from collections import deque

RATE_WINDOW = 0.08       # seconds of angle history; rejects 0.1-degree quantization
MIN_RATE_SPAN = 0.04
MAX_SAMPLE_GAP = 0.10
ANGLE_QUANTUM = 0.10     # degrees, K5 SAS_Angle resolution
MAX_WHEEL_RATE = 300.0   # deg/s; reject discontinuous/reset sensor samples
PREVIEW_TIME = 0.12      # seconds, capped by the available delay-buffer history
MAX_ACCEL_CREDIT = 0.12  # m/s^2 of error; never credit away a large tracking gap
MIN_CORRECTION_SCALE = 0.35


class SteeringMotionCredit:
  def __init__(self):
    self.reset()

  def reset(self):
    self._angles = deque(maxlen=32)
    self._direction = 0.0
    self.rate_deg = 0.0
    self.scale = 1.0
    self.credit = 0.0

  def observe(self, angle_deg: float, mono_t: float) -> float:
    # Do NOT use CS.steeringRateDeg here. On this K5 it is SAS_Speed:
    # UNSIGNED, quantized in 4 deg/s steps. Its sign cannot distinguish a
    # wheel entering a left turn from one unwinding that same turn.
    if not (math.isfinite(angle_deg) and math.isfinite(mono_t)):
      self.reset()
      return 0.0
    if self._angles:
      last_t, last_angle = self._angles[-1]
      dt = mono_t - last_t
      delta = angle_deg - last_angle
      if dt <= 0.0 or dt > MAX_SAMPLE_GAP or abs(delta) > MAX_WHEEL_RATE * dt:
        self.reset()
      elif delta != 0.0:
        direction = math.copysign(1.0, delta)
        if direction != self._direction:
          # A real direction reversal invalidates old motion credit at once.
          self._angles.clear()
          self._angles.append((last_t, last_angle))
        self._direction = direction
    self._angles.append((mono_t, angle_deg))
    while len(self._angles) > 2 and mono_t - self._angles[1][0] >= RATE_WINDOW:
      self._angles.popleft()
    span = mono_t - self._angles[0][0]
    travel = angle_deg - self._angles[0][1]
    # Subtract one encoder quantum: a single quantized tick must never be
    # projected into a claimed ongoing movement. Bias toward less credit.
    self.rate_deg = (math.copysign(max(abs(travel) - ANGLE_QUANTUM, 0.0), travel) / span
                     if span >= MIN_RATE_SPAN else 0.0)
    return self.rate_deg

  def correction_scale(self, error: float, measured_rate: float, reference_travel: float,
                       preview_time: float, v_ego: float, enabled: bool = True) -> float:
    """Rates/travel/error are in lateral-accel units, using the same sign.

    Reference travel is future delayed setpoint minus current delayed setpoint,
    not a derivative of the model's next update. That future is already known.
    """
    self.scale, self.credit = 1.0, 0.0
    if (not enabled or not all(math.isfinite(x) for x in (error, measured_rate, reference_travel, preview_time, v_ego)) or
        preview_time <= 0.0 or preview_time > PREVIEW_TIME + 1e-9 or v_ego <= 5.0 or error * measured_rate <= 0.0):
      return self.scale

    direction = math.copysign(1.0, error)
    surplus = max(0.0, direction * (measured_rate * preview_time - reference_travel))
    speed_weight = min((v_ego - 5.0) / 5.0, 1.0)
    credit_limit = min(MAX_ACCEL_CREDIT, (1.0 - MIN_CORRECTION_SCALE) * abs(error))
    # Smoothly spend only part of the demonstrated surplus; no hard switch
    # between full feedback and its floor as the wheel approaches the target.
    self.credit = speed_weight * credit_limit * surplus / (abs(error) + surplus + 1e-9)
    self.scale = 1.0 - self.credit / abs(error)
    return self.scale
