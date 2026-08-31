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

from openpilot.selfdrive.controls.lib.lead_physics import LEAD_DECEL_MAX

# Jerk toward more accel (throttle apply / brake release), m/s^3.
# Passenger-comfortable range is ~0.9-2.5 m/s^3; personality picks the point.
JERK_UP_DEFAULT = 1.8

# Jerk toward less accel (throttle release / brake apply), m/s^3.
# Interpolated on the DEMANDED accel so a strong braking request immediately
# unlocks a high slew rate — the comfort cap only applies to mild demands.
#
# FunnyPilot v3.5.3 EXTENDED THE TABLE INTO THE POSITIVE REGION. `np.interp`
# CLAMPS outside its breakpoints, so with the old two-point table EVERY target
# above -1.0 got 4.0 m/s^3 — including simply lifting off the throttle at +1.0
# with nothing wrong, which took the car from full throttle to zero in a
# quarter of a second. That is the single most-felt harshness on an ordinary
# highway mile, and it was an artefact of the clamp rather than a decision.
#
# THIS CANNOT WEAKEN BRAKING, and the reason is the interpolation variable: the
# lookup is on the DEMAND, not on the current output. The moment the planner
# asks for -2.0 the table returns ~9.5 on that very frame, whatever the shaper
# was doing before. The relaxed values are reachable only while the demand
# itself is mild. Keep the sequence MONOTONICALLY DECREASING — a later edit
# that raises a value in the middle would make firmer braking gentler, which is
# the one thing this module promises never to do (test_jerk_down_is_monotone).
#
# FunnyPilot v3.5.5 PULLED THE RELAXATION OUT OF THE NEGATIVE REGION. v3.5.3
# put 3.0 at a demand of 0.0, which meant every demand between -1.0 and 0 was
# slewed more slowly than before — and that band is precisely "ease off for a
# lead that is slowing". The intent was only ever to soften a THROTTLE LIFT, so
# the relaxation now begins at 0 and the whole demand <= 0 half of the table is
# bit-identical to the pre-v3.5.3 constant. A demand of +1.0 still gets 2.5.
JERK_DOWN_BP = [-3.5, -1.0, 0.0, 1.0]
JERK_DOWN_V = [12.0, 4.0, 4.0, 2.5]


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
    self._dn = 0
    self.stopping_lead = False
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


# ── approaching something that is stopped or crawling (v3.6.6) ─────────────
#
# THE REPORTED FAILURE: "in stop and go city traffic it frequently decides it
# doesn't need to brake early for a stopped car, and is very much willing to
# rear end someone if I don't take over braking."
#
# WHY THE MPC ALONE IS NOT ENOUGH HERE, and this is the honest part. Its
# following constraint is a COST, not a hard limit: `get_safe_obstacle_distance`
# says how far back it would LIKE to be and the solver trades that against
# comfort, the model's own plan and the cruise obstacle. In `blended` mode the
# model's velocity plan carries a weight of 5.0 against 0.1 on position, so a
# stopped car the model has not committed to is a soft suggestion. That is
# exactly the case that hurts: a stationary obstacle gives no relative-motion
# cue, and the failure mode is a rear-end rather than a late brake.
#
# WHAT THIS ADDS is the fork's own idiom applied to that one case: a
# SPEED-DOMAIN cap, computed from stopping geometry rather than from a cost.
# "At this distance, how fast may I be going and still stop comfortably behind
# it?" — `v = sqrt(v_lead^2 + 2*a*(d - gap))`. It goes through the governor's
# min() into `v_cruise`, which every MPC mode honours (cruise obstacle in acc,
# position cap in blended), so it works where the cost trade-off does not.
#
# IT CAN ONLY EVER SLOW THE CAR. It never raises a target, never touches the
# accel path, and the MPC's own lead constraint still owns the actual stop —
# this is an early speed shave, not the emergency brake.
#
# 1.8 m/s^2 is the deceleration this cap is willing to PLAN on. Deliberately
# gentler than COMFORT_BRAKE (2.2): the point is to be slow enough early that
# the MPC never has to find the hard part, so planning on a gentle rate makes
# the cap bind SOONER, which is the whole idea.
STOP_GOV_A = 1.8
# Where we intend to be stopped behind it. Roomier than long_mpc.STOP_DISTANCE
# (7.5) because this is a cap taking effect hundreds of metres out, and the
# distance it is aiming at should be the comfortable one rather than the
# closest legal one.
STOP_GOV_GAP_M = 9.0
# Only for a lead that is stopped or crawling. Above this the ordinary
# following problem is the MPC's and this must not join in — a general
# follow-distance governor competing with the solver is exactly the stacked-
# authority mistake v3.2.6e removed.
STOP_GOV_LEAD_V = 6.0
# ...faded rather than switched, over the last third of that band, so a lead
# accelerating away does not step the cap off.
STOP_GOV_FADE_V = 4.0
# Consecutive frames of a tracked slow lead before the cap engages. A radar
# ghost at 30 m would otherwise drop the cruise target for one frame.
STOP_GOV_CONFIRM_N = 3
# How fast the cap may FALL, m/s per second. This is a supplementary early
# shave and the MPC's lead constraint is the real one, so it does not need to
# arrive instantly — and rate-limiting it is what stops a flickering track from
# yanking the cruise target.
STOP_GOV_FALL_RATE = 3.0
# v3.6.7 — IT ONLY ACTS WHILE WE ARE ACTUALLY CLOSING, AND WITHOUT THIS IT WAS A
# LAUNCH BUG. The envelope evaluates to exactly `v_lead` at `d == GAP`, so
# sitting 6 m behind a car in traffic capped the cruise target at the lead's own
# speed — the car could match it and never regain a normal gap. Reported as "the
# lead car drives from a stop and our vehicle doesn't accelerate appropriately",
# and it was mine, introduced in v3.6.6.
#
# The fix is to say what this governor is FOR. It answers "am I going too fast
# for what is in front of me", which is only a question while the gap is
# shrinking. At or below the lead's speed there is nothing to shave and the
# ordinary following problem is the MPC's, exactly as it is for a fast lead.
# Two thresholds so a car tracking a lead at a near-identical speed cannot
# chatter the governor on and off.
STOP_GOV_CLOSE_ON = 1.0    # m/s of overspeed on the lead to ENGAGE
STOP_GOV_CLOSE_OFF = 0.2   # ...and to keep holding once engaged

# ── FunnyPilot v3.6.8: a lead COMMITTED TO STOPPING, not merely already slow ──
#
# `STOP_GOV_LEAD_V` 6.0 meant the governor could not act on a car braking for a
# red light until it was ALREADY down to 6 m/s — by which point most of the
# geometry has been spent and the MPC is the only thing left. Reported as "too
# close to lead cars when approaching a stop or slowing down, to the point I'm
# taking over braking before the longitudinal stack even begins acting on the
# lead's deceleration".
#
# WHAT MAKES ARMING EARLIER SAFE IS THE ENVELOPE, NOT A THRESHOLD. v3.6.6 was
# right that a general follow-distance governor competing with the solver is the
# stacked-authority mistake v3.2.6e removed, so the discriminator has to be
# "this lead is stopping", not "this lead is slower than me". Where the lead is
# provably braking, the honest geometry is not its CURRENT speed but WHERE IT
# WILL STOP:
#
#     d_eff = d_rel + v_lead^2 / (2 * decel_lead)      # the lead's stop point
#     cap   = sqrt(2 * STOP_GOV_A * (d_eff - GAP))     # ...and ours behind it
#
# THAT EXPRESSION SELF-LIMITS, which is the whole safety argument and it is
# structural rather than tuned. A lead easing off gently projects its stop a
# long way ahead, so the cap comes out far above cruise and changes nothing:
# measured, a lead at 29 m/s braking 0.8 m/s^2 with a 50 m gap projects a stop
# 576 m away and yields a 45 m/s cap — no effect at any legal speed. The cap
# only descends to meet us when the stop is genuinely close. A lead braking
# 2.5 m/s^2 from 20 m/s first binds around a 45 m gap and tightens from there.
#
# DECEL_MIN 1.2 m/s^2 is "a brake application", comfortably above coasting
# (~0.3-0.5) and above ordinary traffic modulation. It is a gate on ARMING; the
# envelope above is what decides whether anything actually happens.
STOP_GOV_DECEL_MIN = 1.2
# ...and how hard we are willing to believe the lead is braking when projecting
# its stop. Imported rather than retyped: `aLeadK` is a Kalman output and an
# uncapped spike would project the lead's stop right on top of us for one
# frame, which is the brake jab lead_physics.py already bounds for the MPC.
STOP_GOV_DECEL_MAX = LEAD_DECEL_MAX
# Consecutive frames of observed braking before the lead counts as committed.
# Separate from CONFIRM_N because that debounces the TRACK and this debounces
# the CLAIM ABOUT IT; a lead can be solidly tracked and still be one noisy
# accel sample away from looking like it is stopping.
STOP_GOV_DECEL_N = 4


class StopGovernor:
  """Cap the cruise speed at what still stops comfortably behind a slow lead.

  Speed-domain, monotone in distance, and floored at the lead's own speed so it
  can never ask the car to go slower than the thing it is following.
  """

  def __init__(self, dt: float):
    self.dt = dt
    self._dn = 0
    self.stopping_lead = False
    self.reset()

  def reset(self) -> None:
    """Drop the CAP. Deliberately does not clear the braking observation.

    v3.6.8 — `_dn` counts frames of observed lead braking, and the arming test
    reads the result of that count. Clearing it here made the two circular: the
    governor could not arm without the count, and the count was wiped on every
    frame it was not armed. Found by `test_a_braking_lead_arms_it_well_above_
    the_crawl_threshold`, which is the whole reason to test the arming path
    rather than only the envelope.
    """
    self._n = 0
    self.cap = None          # None = not constraining

  def _forget_lead(self) -> None:
    """Everything reset() drops, plus what we believed about the lead itself.
    Used when the track is gone — a new lead is not the old one."""
    self.reset()
    self._dn = 0
    self.stopping_lead = False

  def raw_cap(self, d_rel: float, v_lead: float, decel_lead: float = 0.0) -> float:
    """The stopping envelope, before confirmation or rate limiting.

    v3.6.8 — `decel_lead` > 0 means the lead is committed to stopping, and the
    envelope is then measured to WHERE IT WILL STOP rather than to where it is.
    Passing 0 (the default) is exactly the pre-v3.6.8 expression, so every
    caller that has no opinion about the lead's braking gets the old answer.
    """
    if not (np.isfinite(d_rel) and np.isfinite(v_lead)):
      return float('inf')
    vl = max(float(v_lead), 0.0)
    d = float(d_rel)
    here = float(np.sqrt(vl * vl + 2.0 * STOP_GOV_A * max(0.0, d - STOP_GOV_GAP_M)))
    if not (np.isfinite(decel_lead) and decel_lead >= STOP_GOV_DECEL_MIN):
      return here
    dec = min(float(decel_lead), STOP_GOV_DECEL_MAX)
    # The lead's own stopping distance, added to the gap: this is where the back
    # of it comes to rest, which is the thing we actually have to stop behind.
    d_stop = d + (vl * vl) / (2.0 * dec)
    there = float(np.sqrt(2.0 * STOP_GOV_A * max(0.0, d_stop - STOP_GOV_GAP_M)))
    # THE MIN IS LOAD-BEARING AND IT WAS FOUND BY A TEST, NOT BY REVIEW. The two
    # forms cross over: at range the projection is much tighter, because a
    # bounded roll-out replaces the `v_lead^2` credit; up close it is LOOSER,
    # because it credits us with room the lead only earns by continuing to
    # move. Believing a lead is stopping must never be a reason to go faster at
    # it, so the answer is whichever is smaller, and that property is
    # structural rather than a happy consequence of the constants.
    return min(here, there)

  def update(self, lead_status: bool, d_rel: float, v_lead: float,
             v_ego: float, v_cruise: float, a_lead: float = 0.0) -> float:
    tracked = bool(lead_status) and np.isfinite(v_lead)
    # v3.6.8 — a lead is "committed to stopping" only after DECEL_N consecutive
    # frames of real braking. Counted rather than filtered so the requirement is
    # a duration and not a magnitude: a single hard sample must not arm it, and
    # a lead braking steadily but modestly must.
    if not tracked:
      self._forget_lead()
      return v_cruise
    braking = np.isfinite(a_lead) and -float(a_lead) >= STOP_GOV_DECEL_MIN
    self._dn = self._dn + 1 if braking else 0
    self.stopping_lead = self._dn >= STOP_GOV_DECEL_N
    slow = float(v_lead) <= STOP_GOV_LEAD_V or self.stopping_lead
    # Hysteresis on the engage threshold, not on the cap: which question is
    # being asked must not flicker.
    margin = STOP_GOV_CLOSE_OFF if self.cap is not None else STOP_GOV_CLOSE_ON
    closing = np.isfinite(v_ego) and float(v_ego) > float(v_lead) + margin
    if not (slow and closing):
      self.reset()
      return v_cruise

    self._n += 1
    if self._n < STOP_GOV_CONFIRM_N:
      return v_cruise

    raw = self.raw_cap(d_rel, v_lead, -float(a_lead) if self.stopping_lead else 0.0)
    # THE FADE IS ON THE LEAD'S SPEED, NOT ON THE CAP. Weighting the cap itself
    # toward v_cruise would make a distant slow lead look faster than it is;
    # weighting the AUTHORITY leaves the geometry honest and simply gives it
    # less say as the lead speeds up and the problem becomes the MPC's again.
    # ...and NOT faded at all for a lead that is committed to stopping: the
    # fade exists because a lead merely crawling may be about to accelerate
    # away, which is not true of one under the brakes. Fading a committed
    # stopper would remove the authority exactly where v3.6.8 adds it.
    w = 1.0
    if not self.stopping_lead and float(v_lead) > STOP_GOV_FADE_V:
      span = max(1e-3, STOP_GOV_LEAD_V - STOP_GOV_FADE_V)
      w = float(np.clip((STOP_GOV_LEAD_V - float(v_lead)) / span, 0.0, 1.0))
    target = min(v_cruise, raw + (1.0 - w) * max(0.0, v_cruise - raw))

    if self.cap is None:
      self.cap = max(target, float(v_ego))   # seed at the current speed, no step
    else:
      self.cap = max(target, self.cap - STOP_GOV_FALL_RATE * self.dt)
    self.cap = min(self.cap, v_cruise)
    return min(v_cruise, self.cap)


# ════════════════════════════════════════════════════════════════════════════
# FunnyPilot v3.6.7 — WHO IS DECIDING THE LONGITUDINAL, AS ONE NUMBER.
#
# There are five things that can be the binding constraint on this stack and
# they live in four different files: the MPC's lead obstacle, the MPC's cruise
# obstacle, the model's own plan in blended mode, whichever speed governor won
# the min(), and the stop governor. The dev UI must not re-derive that — two
# copies of a selection rule drift the moment either is tuned, and a readout
# that disagrees with the controller is worse than no readout (the v3.5.0 rule
# that put `gov_lat`/`gov_lon` on the wire in the first place).
#
# So the planner classifies it, here, in a pure function that can be tested,
# and publishes the code. ORDER IS THE WHOLE CONTENT OF THIS FUNCTION and it
# runs from the most immediate authority outward:
#
#   LEAD   the MPC's lead obstacle is nearest — nothing upstream matters,
#          because a cruise cap only ever lowers a target the lead is already
#          holding us under
#   STOP   the stop governor is what put v_cruise where it is
#   SCCV / SCCM / SLA   a speed governor won the min()
#   E2E    blended mode, the model's own plan is binding
#   CRZ    nothing is binding; we are holding the set speed
SRC_CRUISE = 0
SRC_LEAD = 1
SRC_SCC_V = 2
SRC_SCC_M = 3
SRC_SLA = 4
SRC_STOP = 5
SRC_E2E = 6

SRC_NAMES = {SRC_CRUISE: "CRZ", SRC_LEAD: "LEAD", SRC_SCC_V: "SCCV",
             SRC_SCC_M: "SCCM", SRC_SLA: "SLA", SRC_STOP: "STOP",
             SRC_E2E: "E2E"}

# How close the stop governor's cap has to be to the cruise target actually
# used before we credit it with the constraint. It writes v_cruise through a
# min(), so equality is the test; the tolerance is float slack, not a band.
_SRC_EPS = 1e-3


def long_source_code(mpc_source: str, gov_source: str, stop_cap, v_cruise: float) -> int:
  """Classify the binding longitudinal constraint. Pure; never raises.

  mpc_source: `LongitudinalPlanner.mpc.source` ('lead0'/'lead1'/'cruise'/'e2e')
  gov_source: the SP planner's own source ('cruise'/'sccVision'/'sccMap'/
              'speedLimitAssist'), i.e. which governor won the min()
  stop_cap:   `StopGovernor.cap`, None when it is not constraining
  v_cruise:   the cruise target as finally handed to the MPC
  """
  m = str(mpc_source)
  if m in ("lead0", "lead1"):
    return SRC_LEAD
  try:
    if stop_cap is not None and float(stop_cap) <= float(v_cruise) + _SRC_EPS:
      return SRC_STOP
  except (TypeError, ValueError):
    pass
  g = str(gov_source)
  if g == "sccVision":
    return SRC_SCC_V
  if g == "sccMap":
    return SRC_SCC_M
  if g == "speedLimitAssist":
    return SRC_SLA
  if m == "e2e":
    return SRC_E2E
  return SRC_CRUISE
