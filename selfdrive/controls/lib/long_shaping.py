"""FunnyPilot v3.2.6e — longitudinal output shaping.

Single-authority comfort shaping for the longitudinal planner. Design rules,
in priority order (what an autonomous vehicle owes its passengers):

  1. SAFETY IS NEVER COMFORT-LIMITED. The down-jerk allowance grows with the
     strength of the braking demand, so a hard demand is executed essentially
     unshaped, and the caller can bypass shaping entirely (FCW). The shaper
     can therefore only ever soften throttle, never dilute or delay braking
     the planner asked for.
  2. COMFORT IS ENFORCED IN EXACTLY ONE PLACE. Throttle application and brake
     release are jerk-limited here and nowhere else — no stacked filters or
     gates whose interactions can't be reasoned about.
  3. ROBUSTNESS LIVES IN THE SPEED DOMAIN. LeadGrace handles radar lead
     flicker and lead departures by capping the cruise speed target; by
     construction its cap is >= v_ego, so it can hold the car back but can
     never command braking.

This module is import-light (numpy only) so its tests run without the full
openpilot environment.
"""
import numpy as np

# Jerk toward more accel (throttle apply / brake release), m/s^3.
# Passenger-comfortable range is ~0.9-2.5 m/s^3; personality picks the point.
JERK_UP_DEFAULT = 1.8

# Jerk toward less accel (throttle release / brake apply), m/s^3.
# Interpolated on the DEMANDED accel so a strong braking request immediately
# unlocks a high slew rate — the comfort cap only applies to mild demands.
JERK_DOWN_BP = [-3.5, -1.0]
JERK_DOWN_V = [12.0, 4.0]


class AccelJerkShaper:
  """Asymmetric jerk limiter on the planner's output accel target."""

  def __init__(self, dt: float, a_init: float = 0.0):
    self.dt = dt
    self.a = float(a_init)

  def reset(self, a: float) -> None:
    self.a = float(a)

  def update(self, a_target: float, jerk_up: float = JERK_UP_DEFAULT, bypass: bool = False) -> float:
    if bypass or not np.isfinite(a_target):
      # FCW / emergency: execute the demand unshaped and re-seed from it.
      self.a = float(a_target) if np.isfinite(a_target) else 0.0
      return self.a
    jerk_down = float(np.interp(a_target, JERK_DOWN_BP, JERK_DOWN_V))
    lo = self.a - jerk_down * self.dt
    hi = self.a + jerk_up * self.dt
    self.a = float(np.clip(a_target, lo, hi))
    return self.a


# LeadGrace timing. ARM: how long a lead must be tracked (while it is the MPC's
# active constraint) before its loss triggers a grace hold — a lead seen for
# only a few frames is likely noise and gets no authority. HOLD: cap cruise at
# the lead's last speed, riding out radar flicker. RELEASE: linear ramp of the
# cap back up to the true cruise target.
LEAD_GRACE_ARM_TIME = 1.0      # s
LEAD_GRACE_HOLD_TIME = 1.5     # s
LEAD_GRACE_RELEASE_TIME = 2.0  # s


class LeadGrace:
  """Speed-domain smoothing of lead loss.

  When a lead we were actually following disappears (radar flicker or a real
  departure), cap v_cruise at the lead's last known speed for HOLD seconds,
  then ramp the cap out over RELEASE seconds. The cap is floored at v_ego so
  this stage can suppress a surge but can never cause braking.
  """

  def __init__(self, dt: float):
    self.dt = dt
    self.reset()

  def reset(self) -> None:
    self._tracked_t = 0.0
    self._lost_t = 0.0
    self._v_hold = None

  def update(self, lead_status: bool, following: bool, v_lead: float, v_ego: float, v_cruise: float) -> float:
    if lead_status:
      self._tracked_t += self.dt
      self._lost_t = 0.0
      if following and self._tracked_t >= LEAD_GRACE_ARM_TIME and np.isfinite(v_lead):
        self._v_hold = max(float(v_lead), 0.0)
      elif not following:
        # Lead present but not constraining us (e.g. far ahead): no authority.
        self._v_hold = None
      return v_cruise

    self._tracked_t = 0.0
    if self._v_hold is None:
      return v_cruise

    self._lost_t += self.dt
    floor = max(self._v_hold, v_ego)  # never command below current speed
    if self._lost_t <= LEAD_GRACE_HOLD_TIME:
      cap = floor
    elif self._lost_t <= LEAD_GRACE_HOLD_TIME + LEAD_GRACE_RELEASE_TIME:
      frac = (self._lost_t - LEAD_GRACE_HOLD_TIME) / LEAD_GRACE_RELEASE_TIME
      cap = floor + frac * max(0.0, v_cruise - floor)
    else:
      self._v_hold = None
      return v_cruise
    return min(v_cruise, cap)
