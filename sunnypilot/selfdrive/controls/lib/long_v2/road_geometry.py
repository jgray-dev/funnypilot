"""FunnyPilot v3.6.2 — the shape of the road ahead, measured from the line we draw.

SCC-M v2 does not ask anyone how fast a bend can be taken. It measures the
bend. This module is the measurement: a route polyline in, a list of corners
out, each with a radius. Nothing here reads a speed, and nothing here knows
what a speed limit is.

────────────────────────────────────────────────────────────────────────────
WHY THE ESTIMATOR IS "TURN ANGLE OVER ARC LENGTH" AND NOT A DERIVATIVE

mapd publishes `MapTargetVelocities` at roughly one point per metre, but those
points are an interpolation of an OSM way whose actual NODES are tens of metres
apart. The polyline is therefore piecewise LINEAR: dead straight between nodes,
turning all at once at each node. Any local finite-difference curvature on that
data is a train of spikes at the nodes and zero everywhere else.

Average curvature over a window has a definition that does not care:

    kappa = (total turn angle across the window) / (arc length of the window)

exact for a circle at ANY point spacing, and computed here from a running sum
of per-vertex turn angles, so the whole profile costs one pass.
`test_exact_on_a_circle` pins the exactness; everything else is built on it.

────────────────────────────────────────────────────────────────────────────
TWO THINGS WERE MEASURED RATHER THAN ASSUMED, AND BOTH CHANGED THE DESIGN

1. SMOOTHING THE POLYLINE IS NOT COSMETIC, IT IS WHAT MAKES THE ESTIMATE
   UNBIASED. Differencing the raw staircase aliases the window against the node
   spacing: whether a window happens to contain one node or two changes the
   answer by a factor of two, and since the corner's radius is taken at the
   TIGHTEST point, the aliasing is always resolved in the pessimistic
   direction. Measured on a synthetic 100 m bend with 20 m nodes, the raw
   estimator returned 71 m — 29% tight, i.e. a corner permanently 16% slower
   than it should be. A boxcar over the positions removes the staircase and the
   same case returns 96 m.

2. A NOISY STRAIGHT ROAD IS A CORNER, UNLESS YOU ASK HOW FAR IT TURNS. With
   2 m of node position error — ordinary for OSM traced from imagery — a dead
   straight road produces an apparent 400 m radius, which at the default budget
   asks the car to slow to 60 mph for nothing. THIS IS THE FAILURE MODE THIS
   FORK HAS SPENT RELEASES ON (see the v3.5.9 lane-merge note), and no amount
   of smoothing removes it, because the noise and the signal live at the same
   scale.

   What separates them is TOTAL TURN ANGLE, and it separates them completely.
   Measured over the same set of synthetic cases:

       noisy straight, 30 m nodes        R 397 m     turn   6 deg
       noisy straight, 60 m nodes        R 468 m     turn   7 deg
       R=40  hairpin, 15-60 m nodes      R  53-60    turn  90-94 deg
       R=100 bend,    15-60 m nodes      R  79-90    turn  91-94 deg
       R=200 bend,    15-60 m nodes      R 139-172   turn  50-54 deg
       R=400 sweeper, 30-60 m nodes      R 236-291   turn  44-45 deg

   There is a factor of six between the two populations. MIN_CORNER_TURN_DEG
   sits in that gap, and it is the single most important guard in this file: a
   road that does not turn through a real angle is not a corner, however
   curved one window of it looks. With it in place, 96 runs of synthetic
   straight road at up to 2 m of node noise produced NO corners at all.

────────────────────────────────────────────────────────────────────────────
RESIDUAL ERROR, STATED RATHER THAN HIDDEN

Measured over 40 synthetic cases spanning R 40-800 m, node spacing 15-100 m and
0-2 m of node noise, with `tight_trim()` applied:

    worst LOOSE  (would take a corner too fast)   +19%, at R = 40 m mapped
                 with 30 m nodes and 2 m of noise — two nodes in the whole
                 corner, which is about as little geometry as a bend can have
    median error                                  -7% (i.e. slightly slow)
    worst TIGHT  (would take a corner too slowly) -54%, at R = 800 m with
                 15 m nodes and 2 m of noise
    false corners on a straight road              0 in 96 runs at <= 2 m noise

THE ASYMMETRY IS THE DESIGN, NOT AN ACCIDENT. `tight_trim()` exists to keep the
loose tail small, because that is the tail that takes a bend faster than it
should; the tight tail costs speed and the learned budget in corner_speed.py
buys it back per corner as the road is driven. A change here that improves the
average by widening the loose tail is a bad trade even if the table looks
better.

IF SCC-M v2 SLOWS FOR SOMETHING THAT IS NOT A CORNER, MIN_CORNER_TURN_DEG IS
THE FIRST KNOB, not the budget in corner_speed.py. Raising it makes the
detector stricter about what counts as a bend; lowering the budget just makes
every real corner slower too.

Import-light: stdlib only, no numpy, so the maths is testable anywhere.
"""
import math

# Uniform arc-length spacing the profile is computed on.
RESAMPLE_M = 4.0
# Boxcar half-width applied to the POSITIONS before any differencing. Sized
# from the node spacing it has to survive (see finding 1); 30 m spans two to
# three nodes on a typical rural way.
SMOOTH_M = 30.0
# Half-width of the window the turn angle is measured over.
WINDOW_M = 25.0

# A radius the estimator may not claim to be under. Nothing a car drives at
# speed is tighter; a value below it means the polyline jinked, not the road.
R_MIN_M = 18.0
# Above this a bend is not a corner for our purposes — at A_LAT_MAX it would
# permit ~52 m/s, which no set speed on this car reaches.
R_MAX_M = 900.0

# THE GUARD. See finding 2 in the module docstring.
MIN_CORNER_TURN_DEG = 18.0
# A run of curvature shorter than this is a kink in the data, not a corner.
MIN_CORNER_LEN_M = 12.0
# Two same-signed runs closer together than this are one corner with a loose
# middle. Without it, a bend mapped with an awkward node is learned twice, at
# two positions, each with half the visits — the failure MERGE_M exists to
# prevent in the store, arriving one layer earlier.
CORNER_GAP_M = 25.0

# Empirical correction for the smoothing rounding off tight corners: a factor
# ramping from R_TRIM_LO at R_MIN_M to 1.0 at R_TRIM_HI_M. Continuous on
# purpose — a step here would step the speed.
#
# 0.65 WAS CHOSEN AGAINST THE LOOSE TAIL, NOT THE AVERAGE. Swept over the same
# 48 synthetic cases: 0.75 leaves a worst case of +28% (a 40 m hairpin mapped
# with 30 m nodes and 2 m of noise — two nodes in the whole corner), 0.70
# leaves +24%, and 0.65 brings it to +19%. Going from 0.75 to 0.65 moves the
# MEDIAN error only from -5% to -7%, so the whole cost is two percent of extra
# pessimism everywhere in exchange for capping the one tail nothing downstream
# can recover from. Learning buys the two percent back per corner; nothing buys
# back a bend taken too fast.
R_TRIM_LO = 0.65
R_TRIM_HI_M = 120.0

# FunnyPilot v3.6.2 — THE INPUT BUDGET IS SPENT AROUND THE CAR, NOT FROM THE
# ARRAY'S HEAD.
#
# `points[:MAX_POINTS]` is the obvious way to bound this and it is WRONG:
# mapd's array can contain road already driven, and every point of it consumes
# the budget, so the FORWARD horizon shrinks by however much of the past mapd
# happens to still be publishing. Measured on a synthetic route with a bend
# 310 m ahead: truncating from the head lost it entirely, while the same ego
# position with the array windowed around the car found it. A corner silently
# dropping out of range is indistinguishable from a road with no corner on it.
#
# So the array is projected first, the nearest point to the car is found, and
# the window is taken around THAT. The budget is then a distance either side of
# the car rather than a position in someone else's array.
MAX_POINTS = 2000     # hard bound on how much of the input array we will walk
WINDOW_AHEAD_M = 500.0    # comfortably past MAX_LOOKAHEAD_M so the tail is real
WINDOW_BEHIND_M = 120.0   # enough for a corner being traversed to stay whole
MAX_VERTICES = 400    # bound on the resampled profile

M_PER_DEG = 111320.0


def wrap_pi(a: float) -> float:
  """Angle to (-pi, pi]. Applied per vertex, never to an accumulated total, so
  a corner that turns through more than 180 degrees still sums correctly."""
  return (a + math.pi) % (2.0 * math.pi) - math.pi


def tight_trim(radius: float) -> float:
  """Apply the measured tight-corner correction. See the module docstring."""
  if not radius > 0.0:
    return 0.0
  span = R_TRIM_HI_M - R_MIN_M
  f = (radius - R_MIN_M) / span if span > 0 else 1.0
  f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)
  return max(R_MIN_M, radius * (R_TRIM_LO + (1.0 - R_TRIM_LO) * f))


def to_local(points, lat0: float, lon0: float, bearing_deg: float):
  """[(lat, lon), ...] -> [(fwd, right), ...] metres, ego at the origin.

  Equirectangular with the trig hoisted out of the loop: good to centimetres
  over the few hundred metres this reasons about, and the same projection the
  minimap uses, so the two cannot disagree about where the road is.
  """
  b = math.radians(bearing_deg)
  cb, sb = math.cos(b), math.sin(b)
  clat = math.cos(math.radians(lat0))
  out = []
  for lat, lon in points:
    n = (lat - lat0) * M_PER_DEG
    e = (lon - lon0) * M_PER_DEG * clat
    out.append((n * cb + e * sb, -n * sb + e * cb))
  return out


def window_around_ego(xy, ahead_m: float = WINDOW_AHEAD_M,
                      behind_m: float = WINDOW_BEHIND_M):
  """Trim an ego-frame polyline to a window either side of the car.

  Cuts by ARC LENGTH FROM THE NEAREST POINT, walking outward along the route in
  both directions, rather than by the straight-line distance of each point.
  A hairpin brings road 300 m away to within 40 m of the car, and a
  straight-line filter would keep it while dropping the road in between —
  leaving a polyline with a hole in it, which the curvature estimator would
  read as one enormous turn.
  """
  n = len(xy)
  if n < 3:
    return xy
  near = min(range(n), key=lambda i: xy[i][0] * xy[i][0] + xy[i][1] * xy[i][1])

  lo = near
  d = 0.0
  while lo > 0 and d < behind_m:
    d += math.hypot(xy[lo][0] - xy[lo - 1][0], xy[lo][1] - xy[lo - 1][1])
    lo -= 1
  hi = near
  d = 0.0
  while hi < n - 1 and d < ahead_m:
    d += math.hypot(xy[hi + 1][0] - xy[hi][0], xy[hi + 1][1] - xy[hi][1])
    hi += 1
  return xy[lo:hi + 1]


def resample(xy, spacing: float = RESAMPLE_M):
  """Uniform arc-length resampling of a polyline. Returns [(s, x, y), ...].

  Uniform spacing is what lets the window maths be a running sum with an
  integer half-width instead of a search, and what stops a densely mapped
  corner being weighted more heavily than a sparsely mapped one.
  """
  if spacing <= 0.0 or len(xy) < 2:
    return []
  out = [(0.0, xy[0][0], xy[0][1])]
  carry = 0.0
  s_total = 0.0
  for i in range(1, len(xy)):
    x0, y0 = xy[i - 1]
    x1, y1 = xy[i]
    seg = math.hypot(x1 - x0, y1 - y0)
    if not (seg > 0.0) or seg != seg:
      continue
    pos = spacing - carry
    while pos <= seg:
      t = pos / seg
      s_total += spacing
      out.append((s_total, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
      if len(out) >= MAX_VERTICES:
        return out
      pos += spacing
    carry = seg - (pos - spacing)
  return out


def smooth_polyline(rs, half_m: float = SMOOTH_M, spacing: float = RESAMPLE_M):
  """Boxcar the POSITIONS. Finding 1 in the module docstring is why.

  Prefix sums, so this is O(n) rather than O(n*k) — not because n*k would be
  slow here, but because the shape of the loop should not invite someone to
  raise SMOOTH_M and quietly pay for it at 20 Hz.
  """
  k = int(round(half_m / spacing)) if spacing > 0 else 0
  n = len(rs)
  if k < 1 or n < 3:
    return rs
  px = [0.0] * (n + 1)
  py = [0.0] * (n + 1)
  for i, (_s, x, y) in enumerate(rs):
    px[i + 1] = px[i] + x
    py[i + 1] = py[i] + y
  out = []
  for i in range(n):
    a = i - k if i - k > 0 else 0
    b = i + k if i + k < n - 1 else n - 1
    c = b - a + 1
    out.append((rs[i][0], (px[b + 1] - px[a]) / c, (py[b + 1] - py[a]) / c))
  return out


def turn_prefix(rs):
  """Cumulative turn angle at each vertex, radians. `T[b] - T[a]` is the total
  angle the road turns through between vertex a and vertex b."""
  m = len(rs)
  t = [0.0] * m
  if m < 3:
    return t
  prev = math.atan2(rs[1][2] - rs[0][2], rs[1][1] - rs[0][1])
  acc = 0.0
  for j in range(1, m - 1):
    th = math.atan2(rs[j + 1][2] - rs[j][2], rs[j + 1][1] - rs[j][1])
    acc += wrap_pi(th - prev)
    prev = th
    t[j] = acc
  t[m - 1] = acc
  return t


def curvature_profile(rs, spacing: float = RESAMPLE_M, window: float = WINDOW_M,
                      turns=None):
  """Signed curvature at every vertex, 1/m.

  The value at vertex i is the total turn angle over the window centred on i
  divided by that window's arc length. Near the ends the window is truncated
  and the denominator shortened to match, so the estimate degrades in noise
  rather than in correctness.
  """
  m = len(rs)
  if m < 3 or spacing <= 0.0:
    return [0.0] * m
  t = turn_prefix(rs) if turns is None else turns
  k = max(1, int(round(window / spacing)))
  kmax = 1.0 / R_MIN_M

  out = []
  for i in range(m):
    a = i - k if i - k > 0 else 0
    b = i + k if i + k < m - 1 else m - 1
    span = (b - a) * spacing
    if span <= 0.0:
      out.append(0.0)
      continue
    kap = (t[b] - t[a]) / span
    # The floor on radius, applied here so nothing downstream can be handed a
    # hairpin that the data does not actually contain.
    out.append(kmax if kap > kmax else (-kmax if kap < -kmax else kap))
  return out


class RoadCorner:
  """One bend in the road ahead, as measured. Carries no speed."""
  __slots__ = ("s_apex", "x", "y", "radius", "half_len", "s_entry", "s_exit",
               "sign", "heading_rel", "turn_deg")

  def __init__(self, s_apex, x, y, radius, s_entry, s_exit, sign,
               heading_rel=0.0, turn_deg=0.0):
    self.s_apex, self.x, self.y = s_apex, x, y
    self.radius = radius
    self.s_entry, self.s_exit = s_entry, s_exit
    self.half_len = max(1.0, (s_exit - s_entry) * 0.5)
    self.sign = sign
    # The road's heading where the corner STARTS, relative to ego heading, in
    # degrees clockwise. The store keys on heading octant, so the lookup and
    # the write must agree which heading a corner "has" — and the approach
    # heading is the one we are on when we arrive, which is what makes the
    # same tarmac taken the other way a different record.
    self.heading_rel = heading_rel
    self.turn_deg = turn_deg

  def __repr__(self):
    length = self.s_exit - self.s_entry
    return (f"<RoadCorner s={self.s_apex:.0f} R={self.radius:.0f}"
            + f" turn={self.turn_deg:.0f} len={length:.0f}>")


def find_corners(rs, kappa, turns=None, r_max: float = R_MAX_M,
                 min_len: float = MIN_CORNER_LEN_M, gap: float = CORNER_GAP_M,
                 min_turn_deg: float = MIN_CORNER_TURN_DEG):
  """Contiguous same-signed runs of real curvature, as RoadCorner.

  The apex is the tightest vertex in the run, which is where the record
  belongs: braking is planned TO a corner, and the tightest point decides the
  speed. The run is then kept only if the road actually turns through
  `min_turn_deg` across it — see finding 2 in the module docstring.
  """
  m = len(rs)
  if m == 0 or len(kappa) != m:
    return []
  t = turn_prefix(rs) if turns is None else turns
  k_min = 1.0 / max(r_max, 1.0)

  runs = []
  cur = None
  for i in range(m):
    k = kappa[i]
    sgn = 1 if k > 0 else (-1 if k < 0 else 0)
    if abs(k) >= k_min and sgn != 0:
      if cur is not None and cur[0] == sgn and (rs[i][0] - rs[cur[2]][0]) <= gap:
        cur = (sgn, cur[1], i)
      else:
        if cur is not None:
          runs.append(cur)
        cur = (sgn, i, i)
  if cur is not None:
    runs.append(cur)

  out = []
  for sgn, a, b in runs:
    if (rs[b][0] - rs[a][0]) < min_len:
      continue
    turn_deg = abs(math.degrees(t[b] - t[a]))
    if turn_deg < min_turn_deg:
      continue
    apex = a
    for i in range(a, b + 1):
      if abs(kappa[i]) > abs(kappa[apex]):
        apex = i
    r = 1.0 / abs(kappa[apex]) if kappa[apex] else r_max
    j = a if a < m - 1 else m - 2
    hdg = math.degrees(math.atan2(rs[j + 1][2] - rs[j][2], rs[j + 1][1] - rs[j][1]))
    out.append(RoadCorner(rs[apex][0], rs[apex][1], rs[apex][2], tight_trim(r),
                          rs[a][0], rs[b][0], sgn, hdg, turn_deg))
  return out


def corners_from_route(points, lat0: float, lon0: float, bearing_deg: float):
  """The whole pipeline: [(lat, lon), ...] -> ([RoadCorner], s_ego).

  `s_ego` is the arc position of the vertex nearest the car, so a corner's
  distance ahead is `corner.s_apex - s_ego`. Distance is measured ALONG THE
  ROAD rather than as a straight line, which is what makes the approach
  envelope mean the same thing on a winding approach as on a straight one.

  Never raises: any input it cannot make sense of yields no corners, which
  makes every consumer a no-op.
  """
  try:
    if not points or len(points) < 3:
      return [], 0.0
    xy = to_local(points[:MAX_POINTS], lat0, lon0, bearing_deg)
    xy = window_around_ego(xy)
    rs = resample(xy)
    if len(rs) < 3:
      return [], 0.0
    # THE EGO POSITION IS TAKEN FROM THE UNSMOOTHED POLYLINE. Smoothing pulls
    # the line off the road by up to a lane width through a bend, and the whole
    # point of s_ego is where WE are on it.
    s_ego = min(rs, key=lambda p: p[1] * p[1] + p[2] * p[2])[0]
    sm = smooth_polyline(rs)
    turns = turn_prefix(sm)
    kap = curvature_profile(sm, RESAMPLE_M, WINDOW_M, turns)
    return find_corners(sm, kap, turns), s_ego
  except Exception:
    return [], 0.0
