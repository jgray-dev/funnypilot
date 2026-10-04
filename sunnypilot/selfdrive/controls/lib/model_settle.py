"""Bounded model-consistency gate for returning from a driver-signalled turn.

This observes predictions; it never filters or changes the steering command.
Repeated/stale frames cannot satisfy the dwell. A stable curved path is valid.
"""
from collections import deque
import math

MIN_SETTLE_SECONDS = 0.30
MAX_MODEL_AGE = 0.15
MAX_ACCEL_SPREAD = 0.35  # m/s² disagreement across overlapping plans, not only adjacent frames
MAX_HANDOVER_ERROR = 0.5  # m/s² between the action and current measured turn
MAX_HANDOVER_ACCEL = 2.0  # defer takeover in a demanding turn
MAX_PREDICTED_JERK = 1.0  # m/s³; a gently changing curve need not be flat


class ModelSettle:
  def __init__(self):
    self.reset()

  def reset(self):
    self.samples = deque(maxlen=256)  # bounded even with broken timestamps
    self.last_stamp = None

  @staticmethod
  def prediction(model, *, now, received, stamp, valid, speed, measured_curvature, delay=0.0):
    """Validate availability separately from optional early-handover comfort.

    The timeout may bypass prediction disagreement, not a stale/malformed
    model or an active model lane-change maneuver. Downstream engagement,
    calibration, standstill and EPS checks still own actual steering authority.
    """
    if (not valid or not all(math.isfinite(x) for x in (now, received, stamp, speed, measured_curvature, delay)) or
        received <= 0 or stamp <= 0 or speed < 0 or not 0 <= now-received <= MAX_MODEL_AGE):
      raise ValueError('unavailable motion/model')
    if str(model.meta.laneChangeState) != 'off':
      raise ValueError('model maneuver in progress')
    times, rates = list(model.orientationRate.t), list(model.orientationRate.z)
    if len(times) != len(rates) or not 3 <= len(times) <= 33 or times[0] > 0 or times[-1] < 1.0:
      raise ValueError('incomplete plan')
    if not all(math.isfinite(x) for x in times+rates) or any(b <= a for a, b in zip(times, times[1:], strict=False)):
      raise ValueError('invalid plan')

    def at(t):
      for i in range(1, len(times)):
        if times[i] >= t:
          f = (t-times[i-1])/(times[i]-times[i-1])
          return (rates[i-1]*(1-f)+rates[i]*f)*speed
      raise ValueError('short plan')

    prediction = (float(model.action.desiredCurvature)*speed**2, at(0.0), at(0.5), at(1.0))
    if not all(math.isfinite(x) for x in prediction):
      raise ValueError('invalid action')
    return prediction

  @classmethod
  def available(cls, **state):
    try:
      cls.prediction(**state)
      return True
    except (AttributeError, TypeError, ValueError, OverflowError):
      return False

  def update(self, model, *, now, received, stamp, valid, speed, measured_curvature, delay=0.0):
    try:
      prediction = self.prediction(model, now=now, received=received, stamp=stamp, valid=valid,
                                   speed=speed, measured_curvature=measured_curvature, delay=delay)
      if self.last_stamp == stamp:
        return False
      if self.last_stamp is not None and stamp < self.last_stamp:
        self.reset()
      self.last_stamp = stamp
      action = prediction[0]
      if (speed < 2.0 or any(abs(x) > MAX_HANDOVER_ACCEL for x in prediction) or
          abs(action-measured_curvature*speed**2) > MAX_HANDOVER_ERROR or
          abs(action-prediction[1]) > MAX_HANDOVER_ERROR or
          any(abs(b-a)/0.5 > MAX_PREDICTED_JERK for a, b in zip(prediction[1:-1], prediction[2:], strict=True))):
        raise ValueError('turn not ready for early handover')
      if self.samples and received <= self.samples[-1][0]:
        raise ValueError("model receive clock did not advance")
      if self.samples and received-self.samples[-1][0] > MAX_MODEL_AGE:
        self.samples.clear()
      # Compare predictions for the SAME future instant. On a coherent curve
      # transition, the new t=0 is the previous plan's t=elapsed, not its t=0.
      # Unpredicted drift still fails against older overlapping plans; checking
      # only adjacent frames would let a slowly wandering model accumulate error.
      def disagrees(sample):
        when, old = sample
        elapsed = received-when
        # Action can include actuator-delay compensation. Its offset from the
        # near-term path must remain consistent, without requiring a flat path.
        if abs((prediction[0]-prediction[1])-(old[0]-old[1])) > MAX_ACCEL_SPREAD:
          return True
        for horizon, value in ((0.0, prediction[1]), (0.5, prediction[2])):
          t = elapsed+horizon
          if t <= 1.0:
            index = 1 if t <= 0.5 else 2
            fraction = (t-(index-1)*0.5)/0.5
            expected = old[index]*(1-fraction)+old[index+1]*fraction
            if abs(value-expected) > MAX_ACCEL_SPREAD:
              return True
        return False

      if any(disagrees(sample) for sample in self.samples):
        self.samples.clear()
      self.samples.append((received, prediction))
      dwell = max(MIN_SETTLE_SECONDS, delay)
      return len(self.samples) >= 6 and received-self.samples[0][0] >= dwell - 1e-9
    except (AttributeError, TypeError, ValueError, OverflowError):
      self.reset()
      return False
