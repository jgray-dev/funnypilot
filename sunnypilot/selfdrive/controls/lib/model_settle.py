"""Bounded model-consistency gate for returning from a driver-signalled turn.

This observes predictions; it never filters or changes the steering command.
Repeated/stale frames cannot satisfy the dwell. A stable curved path is valid.
"""
from collections import deque
import math

MIN_SETTLE_SECONDS = 0.67
MAX_MODEL_AGE = 0.15
MAX_ACCEL_SPREAD = 0.35  # m/s² across the entire dwell, not only adjacent frames
MAX_HANDOVER_ERROR = 0.5  # m/s² between the action and current measured turn
MAX_HANDOVER_ACCEL = 2.0  # defer takeover in a demanding turn


class ModelSettle:
  def __init__(self):
    self.reset()

  def reset(self):
    self.samples = deque(maxlen=256)  # bounded even with broken timestamps
    self.last_stamp = None

  def update(self, model, *, now, received, stamp, valid, speed, measured_curvature, delay=0.0):
    try:
      if (not valid or not all(math.isfinite(x) for x in (now, received, stamp, speed, measured_curvature, delay)) or
          received <= 0 or stamp <= 0 or not 0 <= now-received <= MAX_MODEL_AGE or speed < 2.0):
        raise ValueError('unavailable motion/model')
      if self.last_stamp == stamp:
        return False
      if self.last_stamp is not None and stamp < self.last_stamp:
        self.reset()
      self.last_stamp = stamp
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
      action = float(model.action.desiredCurvature)*speed**2
      prediction = (action, at(0.0), at(0.5), at(1.0))
      if (not all(math.isfinite(x) and abs(x) <= MAX_HANDOVER_ACCEL for x in prediction) or
          abs(action-measured_curvature*speed**2) > MAX_HANDOVER_ERROR or
          max(prediction)-min(prediction) > MAX_HANDOVER_ERROR):
        raise ValueError('turn not ready for handover')
      if self.samples and received <= self.samples[-1][0]:
        raise ValueError("model receive clock did not advance")
      if self.samples and received-self.samples[-1][0] > MAX_MODEL_AGE:
        self.samples.clear()
      # Restart the dwell at a changed prediction; gradual drift also fails the
      # full-window spread check instead of passing many individually small steps.
      if any(max([p[i] for _, p in self.samples]+[prediction[i]]) -
             min([p[i] for _, p in self.samples]+[prediction[i]]) > MAX_ACCEL_SPREAD for i in range(4)):
        self.samples.clear()
      self.samples.append((received, prediction))
      dwell = max(MIN_SETTLE_SECONDS, delay)
      return len(self.samples) >= 10 and received-self.samples[0][0] >= dwell
    except (AttributeError, TypeError, ValueError, OverflowError):
      self.reset()
      return False
