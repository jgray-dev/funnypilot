"""Comfort-softening dwell and release hysteresis.

Short steeringPressed crossings alone cannot establish driver intent or their
physical cause. This gate avoids repeated comfort scaling on those crossings;
strong opposing torque bypasses it in LatHandback. EPS/Panda limits remain
independent and do not wait for this comfort dwell.
"""

ENGAGE_TIME = 0.4   # s of continuous steeringPressed before softening engages
RELEASE_TIME = 0.3  # s of continuous release before softening lets go


class OverrideGate:
  def __init__(self, dt: float):
    self.dt = dt
    self.reset()

  def reset(self) -> None:
    self.engaged = False
    self._pressed_t = 0.0
    self._released_t = 0.0

  def update(self, pressed: bool) -> bool:
    if pressed:
      self._pressed_t += self.dt
      self._released_t = 0.0
    else:
      self._released_t += self.dt
      self._pressed_t = 0.0

    if not self.engaged and self._pressed_t >= ENGAGE_TIME:
      self.engaged = True
    elif self.engaged and self._released_t >= RELEASE_TIME:
      self.engaged = False
    return self.engaged
