"""FunnyPilot v3.6.2 — from a radius to a speed, and back again.

THE WHOLE OF SCC-M v2's SPEED CHOICE IS ONE LINE:

    v = sqrt(a_lat * R)

`R` is measured from the road (road_geometry.py). `a_lat` is the lateral
acceleration this car is willing to pull through THIS bend, and `a_lat` is the
thing that gets learned. That split is deliberate and it is the answer to "the
chosen speed must be apparent, not emergent": the file on disk holds a number
with units and a meaning, the speed is derived from it in one multiplication,
and if the radius estimate ever improves, every corner already recorded gets
better for free without re-driving it.

────────────────────────────────────────────────────────────────────────────
WHAT IS LEARNED, AND WHY IT IS AN INTERVAL RATHER THAN A NUMBER

A single pass through a corner does not measure "the right speed". It measures
one speed and whether that speed was comfortable. Those are two different
pieces of evidence and they bound the answer from opposite sides:

    a clean pass at a_peak      -> this corner supports AT LEAST a_peak
    a stressed pass at a_peak   -> this corner supports LESS THAN a_peak

So a corner carries a floor `a_lo` (proven supportable) and a ceiling `a_hi`
(proven not supportable), and the estimate is the interval closing from both
ends as the road is driven. A gentle pass never claims the corner is slow — it
simply fails to raise the floor — which is what makes requirement 4 work:
ordinary driving with nothing engaged populates the map honestly, and driving
too fast for the bend produces the ceiling rather than a wrong floor.

────────────────────────────────────────────────────────────────────────────
THE ASYMMETRY IS THE OPPOSITE OF THE OLD STORE'S, FOR THE SAME REASON

scc_learn_store's old `v` was a SPEED, so adopting an increase quickly was the
cautious choice. Here the quantity is `a_lat`, and raising it makes the car go
FASTER. So the floor rises slowly (ALPHA_FLOOR) and the ceiling falls quickly
(ALPHA_CEIL): evidence that we may push harder needs more of itself than
evidence that we must not. Whoever tunes these next should read that sentence
before touching either number — they look symmetric and they are not.

Import-light (stdlib only).
"""
import math

# ── the lateral-acceleration budget ────────────────────────────────────────
#
# 1.8 m/s^2 is DELIBERATELY BELOW SCC-V's own a_lat_target of 2.1. An unvisited
# corner's radius comes from OSM node geometry, and the cost of that being
# wrong should be paid in a slightly slow corner rather than in a fast one.
# Learning is what earns the speed back, and it may earn its way past 2.1 —
# A_LAT_MAX is the ceiling, and it is a comfort bound, not a grip bound: 3.0
# m/s^2 is a firm corner in a family saloon, and well inside what the tyres do.
A_LAT_DEFAULT = 1.8
A_LAT_MIN = 1.0        # a learned ceiling may never argue for less than this
A_LAT_MAX = 3.0        # ...nor a learned floor for more

MIN_V_TARGET = 3.0     # m/s, floor on any cap this module produces

# ── how a pass moves the interval ──────────────────────────────────────────
ALPHA_FLOOR = 0.35     # a clean pass raises the floor by this fraction
ALPHA_CEIL = 0.50      # a stressed pass lowers the ceiling by this fraction
# Only a pass this far under the limit is positive evidence. Mirrors
# corner_effort.CLEAN_TH; a pass between CLEAN_TH and 1.0 moves nothing, which
# is the honest thing for a measurement that says neither.
CLEAN_TH = 0.5

# ── confidence: how much of the learned value actually reaches the cap ─────
# One visit is real evidence, not proof (the same reading v3.5.0 took). But the
# two directions are not treated alike: earning speed needs visits, giving it
# up does not, because "this corner is slower than we thought" is the cheap
# mistake and "this corner is faster than we thought" is the expensive one.
CONF_FIRST = 0.45
CONF_STEP = 0.275
CONF_FULL_VISITS = 3
CONF_LOWER_FLOOR = 0.75   # minimum weight given to a learned value BELOW default

# ── the approach envelope (carried over verbatim from v3.5.6/v3.6.1) ───────
#
# The decel budget INTEGRATED over distance-to-go, so the cap is monotone in
# distance by construction. Evaluating a(d) at each point's own distance is the
# obvious way to write this and it is wrong — the cap it produces can LOOSEN as
# the corner closes, and the car speeds back up mid-approach. See the v3.5.6
# section of CLAUDE.md; this table and the reasoning behind it are unchanged.
#
# 1.20 m/s^2 IS THE CEILING BECAUSE long_mpc.CRUISE_MIN_ACCEL IS -1.2. This is
# a SPEED cap; the MPC cannot follow a steeper envelope. Do not raise one
# without the other.
_J_BP = (0.0, 60.0, 150.0, 400.0)    # m of distance-to-go, after the arrival lead
_J_V = (0.0, 72.0, 144.0, 269.0)     # integral of the budget, m^2/s^2

# Seconds of travel AT THE CORNER SPEED by which the corner speed is reached
# early. v3.6.1 raised this from 2.0: the lead lands us at v_curve exactly
# `v_curve * T` metres before the governing point, and at 2 s that is the apex
# rather than the entry.
ARRIVAL_LEAD_T = 4.0
MAX_LOOKAHEAD_M = 400.0


def _interp(x: float, bp, v) -> float:
  """np.interp for a monotone table, clamping at both ends. Stdlib so this
  module stays importable without numpy."""
  if x <= bp[0]:
    return v[0]
  for i in range(1, len(bp)):
    if x <= bp[i]:
      x0, x1 = bp[i - 1], bp[i]
      y0, y1 = v[i - 1], v[i]
      return y0 + (y1 - y0) * ((x - x0) / (x1 - x0)) if x1 > x0 else y1
  return v[-1]


def clamp(x: float, lo: float, hi: float) -> float:
  return lo if x < lo else (hi if x > hi else x)


def finite(x) -> bool:
  try:
    f = float(x)
  except (TypeError, ValueError):
    return False
  return f == f and abs(f) != float('inf')


def confidence_for(visits: int) -> float:
  """[0, 1] from visit count. 0.45 at one visit, 1.0 at three."""
  try:
    n = int(visits)
  except (TypeError, ValueError):
    return 0.0
  if n <= 0:
    return 0.0
  return min(1.0, CONF_FIRST + CONF_STEP * (min(n, CONF_FULL_VISITS) - 1))


def speed_for(radius_m: float, a_lat: float) -> float:
  """v = sqrt(a_lat * R). The one equation. Returns 0.0 on nonsense."""
  if not (finite(radius_m) and finite(a_lat)) or radius_m <= 0.0 or a_lat <= 0.0:
    return 0.0
  return math.sqrt(a_lat * radius_m)


def lat_accel_for(radius_m: float, v: float) -> float:
  """The inverse: what lateral acceleration a speed implies at this radius."""
  if not (finite(radius_m) and finite(v)) or radius_m <= 0.0:
    return 0.0
  return (v * v) / radius_m


def effective_a_lat(a_lo: float, a_hi: float, visits: int,
                    default: float = A_LAT_DEFAULT) -> float:
  """Blend the learned interval with the default by confidence.

  DIRECTION MATTERS, and this is the whole safety posture of the feature in
  four lines. A learned value ABOVE the default is a claim that we may go
  faster than the geometry alone would allow, and it is weighted by visit count
  so one lucky pass cannot buy it. A learned value BELOW the default is a claim
  that we must go slower, and it is given at least CONF_LOWER_FLOOR weight
  immediately, because withholding a slowdown until the third visit is not a
  conservative choice.

  `min(a_lo, a_hi)` first: a ceiling always beats a floor. If a corner has
  proved it supports 2.4 and later proved it does not support 2.0, the answer
  is 2.0 — the newer, tighter bound — and the floor is stale.
  """
  lo = float(a_lo) if finite(a_lo) else default
  hi = float(a_hi) if finite(a_hi) else A_LAT_MAX
  learned = clamp(min(lo, hi), A_LAT_MIN, A_LAT_MAX)
  c = confidence_for(visits)
  if learned >= default:
    w = c
  else:
    w = max(c, CONF_LOWER_FLOOR)
  return clamp(default + (learned - default) * w, A_LAT_MIN, A_LAT_MAX)


def update_interval(a_lo: float, a_hi: float, a_peak: float, severity: float,
                    seed: bool = False):
  """Fold one observed pass into the interval. Returns (a_lo, a_hi).

  `seed=True` adopts the pass outright instead of easing toward it, and the
  caller passes it for a corner's FIRST visit. THE REASON IS THAT A FRESH
  RECORD'S BOUNDS ARE NOT MEASUREMENTS: `a_hi` starts at A_LAT_MAX and `a_lo`
  at the default because nothing is known yet, and EMA-ing away from those
  treats ignorance as evidence. Without it a first pass that oscillated badly
  at 3.6 m/s^2 would move the ceiling from 3.0 only as far as 2.1, and the
  corner would stay fast for several more visits — the exact case where being
  slow to learn is most expensive.

  `a_peak` is the highest lateral acceleration the car actually pulled through
  the bend; `severity` is corner_effort's verdict, where 1.0 means "exactly at
  the limit". A pass in between CLEAN_TH and 1.0 moves nothing — it is a visit,
  not evidence, and pretending otherwise is how a system drifts.

  THE DIVISION IS THE POINT for severity > 1. It answers "how far past the
  limits were we" with arithmetic: a pass that oscillated at three times the
  threshold while pulling 4.0 m/s^2 says this corner supports about 1.33, which
  is a usable estimate of the right speed from a pass that was nowhere near it.
  Multiplying by a fixed margin instead would say "a bit under 4.0" no matter
  how badly it went, and a very fast pass would teach the car almost nothing —
  which is precisely the case requirement 4 exists to cover.
  """
  lo = float(a_lo) if finite(a_lo) else A_LAT_MIN
  hi = float(a_hi) if finite(a_hi) else A_LAT_MAX
  p = float(a_peak) if finite(a_peak) else 0.0
  s = float(severity) if finite(severity) else 1.0
  if p <= 0.0:
    return clamp(lo, A_LAT_MIN, A_LAT_MAX), clamp(hi, A_LAT_MIN, A_LAT_MAX)

  # THE DIRECTION GUARDS SURVIVE SEEDING. A clean pass is evidence of a lower
  # bound and nothing else, so it may never LOWER the floor however gently the
  # corner was taken; a stressed pass may never RAISE the ceiling. Seeding
  # changes only how fast an admissible move is adopted.
  if s >= 1.0:
    target = clamp(p / max(s, 1.0), A_LAT_MIN, A_LAT_MAX)
    if target < hi:
      hi = target if seed else hi + (target - hi) * ALPHA_CEIL
  elif s < CLEAN_TH:
    target = clamp(p, A_LAT_MIN, A_LAT_MAX)
    if target > lo:
      lo = target if seed else lo + (target - lo) * ALPHA_FLOOR
  return clamp(lo, A_LAT_MIN, A_LAT_MAX), clamp(hi, A_LAT_MIN, A_LAT_MAX)


def approach_cap(v_corner: float, distance_m: float) -> float:
  """The speed permitted NOW for a corner `distance_m` ahead.

  `sqrt(v_corner^2 + 2 * J(d_eff))` with J the integrated decel budget, so the
  cap is monotone in distance and the deceleration implied at distance-to-go s
  is exactly the budget at s.
  """
  if not (finite(v_corner) and finite(distance_m)) or v_corner <= 0.0:
    return float('inf')
  d_eff = max(0.0, distance_m - v_corner * ARRIVAL_LEAD_T)
  return math.sqrt(v_corner * v_corner + 2.0 * _interp(d_eff, _J_BP, _J_V))
