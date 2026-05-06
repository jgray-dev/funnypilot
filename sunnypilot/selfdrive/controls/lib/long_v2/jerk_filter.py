"""
FunnyPilot LongV2 — jerk limiter / first-order accel filter.
"""


class JerkFilter:
  def __init__(self, initial_a: float = 0.0):
    self._a = initial_a

  def reset(self, a: float) -> None:
    self._a = a

  def update(self, a_target: float, dt: float, jerk_limit: float | None = None) -> float:
    if jerk_limit is None:
      from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
      jerk_limit = get_tuning().jerk_limit_normal
    max_delta = jerk_limit * dt
    self._a = max(self._a - max_delta, min(self._a + max_delta, a_target))
    return self._a

  @property
  def value(self) -> float:
    return self._a
