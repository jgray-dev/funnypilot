"""FunnyPilot v3.2.9e — PlanRider: continuous lateral plan resampling.

DEEP RESET of the lateral interpolation stack. Everything since v3.0.2e —
frame counters, time-anchored knot interpolation, phase leads, SETTLE
ease-outs, lookahead weights, health blends — existed to solve one problem:
the model publishes desired curvature at 20 Hz, controls run at 100 Hz, and
naively holding each sample makes the wheel move in 5x-sized "bites".

All of those approaches interpolated between POINT SAMPLES of the model's
plan (action.desiredCurvature = the plan evaluated at lat_delay + DT_MDL).
That is reconstructing a signal we already have: the model publishes its
ENTIRE smooth plan (orientation + orientationRate over 10 s) every frame.

PlanRider instead evaluates the plan itself, continuously:

    every 100 Hz frame:
        t = lat_delay + DT_MDL + (time since this plan was captured)
        desired curvature = get_curvature_from_plan(plan, t)

Between model updates the horizon point advances smoothly ALONG the plan —
the segment from t to t+50 ms is by definition what the model wants the car
doing during the next model period. When a fresh plan arrives, it is
evaluated at the same wall-clock target instant the old plan had reached
(captures are DT_MDL apart, horizons differ by DT_MDL), so handoffs are
continuous up to genuine model revisions — and those are bounded by a
single lateral-jerk clamp (the same 2.5 m/s^3 budget upstream uses).

Properties the old stack needed special machinery for, now by construction:
  * zero added lag — we sample the future of the plan, never filter the past
  * cadence-independent — a late model frame just means riding the current
    plan a little further (it extrapolates the model's own intent, not a
    hold or a stall); staleness beyond RIDE_EXTRA_S degrades to a hold
  * no lane-change special case — there is no ease-out shaping to force off
  * NaN/short plans fall back to the model's own action value, i.e. stock

clip_curvature() in controlsd still runs afterward as the ISO hard limit.
Import-light (numpy + drive_helpers + modeld constants) for containerless
tests.
"""
import numpy as np

from openpilot.selfdrive.controls.lib.drive_helpers import get_curvature_from_plan
from openpilot.selfdrive.modeld.constants import ModelConstants

DT_MDL = 0.05        # s, fixed 20 Hz model period
RIDE_EXTRA_S = 0.20  # s, keep riding a stale plan this far past nominal before holding
MAX_TARGET_LAT_JERK = 2.5  # m/s^3, clamp on output curvature rate (matches upstream budget)
MIN_SPEED = 1.0
HEALTH_FULL = 5.0    # dev-UI scale: 5 = fresh plan, 0 = stale/held

N_PLAN = len(ModelConstants.T_IDXS)
T_IDXS = np.array(ModelConstants.T_IDXS)


class PlanRider:
  def __init__(self, dt: float):
    self.dt = dt
    self.out = 0.0
    self._yaws = None
    self._yaw_rates = None
    self._plan_t = None
    self._last_age = float("inf")

  def reset(self, curvature: float) -> None:
    self.out = float(curvature) if np.isfinite(curvature) else 0.0
    self._yaws = None
    self._yaw_rates = None
    self._plan_t = None
    self._last_age = float("inf")

  @property
  def plan_age(self) -> float | None:
    return None if self._plan_t is None else self._last_age

  @property
  def health_frames(self) -> float:
    """5 while the plan is fresh (age <= one model period), decaying to 0 as a
    stale plan runs out of ride headroom. Feeds the dev-UI INTERP gauge and
    the triage recorder's hmin/havg fields."""
    if self._plan_t is None:
      return 0.0
    over = max(0.0, self._last_age - DT_MDL)
    return HEALTH_FULL * float(np.clip(1.0 - over / RIDE_EXTRA_S, 0.0, 1.0))

  def set_plan(self, yaws, yaw_rates, mono_t: float) -> bool:
    try:
      y = np.asarray(yaws, dtype=float)
      yr = np.asarray(yaw_rates, dtype=float)
      if len(y) != N_PLAN or len(yr) < 1 or not (np.all(np.isfinite(y)) and np.isfinite(yr[0])):
        return False
      self._yaws = y
      self._yaw_rates = yr
      self._plan_t = mono_t
      return True
    except Exception:
      return False

  def update(self, mono_t: float, lat_delay: float, v_ego: float, fallback: float) -> float:
    """Advance along the stored plan; returns the jerk-limited desired curvature."""
    target = None
    if self._plan_t is not None:
      self._last_age = mono_t - self._plan_t
      age = float(np.clip(self._last_age, 0.0, DT_MDL + RIDE_EXTRA_S))
      if self._last_age <= DT_MDL + RIDE_EXTRA_S:
        t = lat_delay + DT_MDL + age
        c = float(get_curvature_from_plan(self._yaws, self._yaw_rates, T_IDXS, max(v_ego, MIN_SPEED), t))
        if np.isfinite(c):
          target = c
      # stale beyond ride headroom: hold self.out (target stays None)
    else:
      self._last_age = float("inf")
      # no plan stored yet: use the model's own action value (stock behavior)
      if np.isfinite(fallback):
        target = float(fallback)

    if target is None:
      return self.out

    # single comfort clamp: bound implied lateral jerk (plan handoffs, model revisions)
    max_step = MAX_TARGET_LAT_JERK / max(v_ego, MIN_SPEED) ** 2 * self.dt
    self.out = float(np.clip(target, self.out - max_step, self.out + max_step))
    return self.out
