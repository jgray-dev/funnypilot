"""FunnyPilot v3.6.2 — the route minimap: SCC-M v2's own input and output, drawn.

WHAT IT SHOWS, AND WHY THAT IS ALL IT SHOWS. The ribbon is the geometry of
`MapTargetVelocities` — the matched route ahead, the identical polyline
`road_geometry.corners_from_route()` measures corner radii from. mapd publishes
exactly eleven params and NONE of them contain side roads: junctions, turnoffs
and surrounding geometry never leave the binary. Drawing them would have meant
querying the offline OSM database ourselves on the UI thread, and it would have
shown the driver geometry the controller cannot see — the opposite of a debug
tool. So the map is one road, which is also why it stays minimal.

THE TINT is the point, and v3.6.2 changed where the number comes from.

Until v3.6.1 it was measured against mapd's own `velocity` field — a corner
speed computed from OSM geometry with someone else's assumptions. SCC-M v2 does
not use that field for anything, so drawing it would have shown a speed the
controller had never heard of. The ribbon is now tinted by the speed THIS CAR
CHOSE for each corner, published over /dev/shm/fp_corners:

    expected = min(set speed, zone limit there x (1 + SLA offset) if SLA on)
    delta    = expected - the speed SCC-M v2 will hold through that corner

so the colour answers "how much will I slow here, and why does the car think
so", which is the question a driver actually has. Road with no corner on it
gets no colour at all: on a straight, SCC-M v2 has nothing to say and the map
now says nothing rather than tinting the road a shade of grey.

THE UI DOES NOT COMPUTE THOSE SPEEDS, AND MUST NOT. Half of each one comes from
the learned lateral budget in a store on /data, and nothing in `hud/` may touch
a filesystem; beyond that, two copies of the geometry-and-learning blend would
drift the moment either was tuned, and a debug readout that disagrees with the
controller is worse than none.

THE CIRCLE marks a corner we have DRIVEN, not the one currently governing —
v3.6.2 replaced the "which point is braking us now" crosshair with "which
points on this ribbon has the car actually learned". It sits over the apex,
one per learned corner from /dev/shm/fp_corners.

ITS OPACITY IS CONVERGENCE, NOT ATTENDANCE, and getting that distinction
wrong was the bug this replaced. The first version used
`confidence_for(visits)`, which saturates at three visits — so a corner whose
speed estimate was still moving 15% on its third pass drew a fully solid
ring, announcing certainty about a number that was still being argued over.
The opacity is now `confidence_of(visits, drift)`: it requires both enough
passes to have evidence AND that evidence having stopped changing its mind.
A corner driven thousands of times whose answer never moves reaches full
opacity; one driven three times that keeps shifting does not, and no number
of further disagreeing passes will get it there.

A corner with zero visits is filtered out entirely — it has no "how sure are
we" to show, because it was priced by geometry alone. A corner with exactly
one visit is drawn at SEEN_MIN_ALPHA: it has converged on nothing yet (there
is no second pass to agree with), but "we have been here" is a real fact and
a different claim from "we know how fast to take it". "LRN" still means the
corner currently governing is one of these.

COST. `MapTargetVelocities` is a JSON array of a few hundred points and the
source data is 1 Hz, so it is parsed at POLL_S and projected into ego-relative
metres ONCE per poll. Per frame the widget only converts metres to pixels and
draws lines. The Params handle is created LAZILY on first onroad draw, never at
import and never in the constructor — /dev/shm/params does not exist offroad
and this module is imported by a process that also draws the offroad screen.
"""
import json
import math
import time

import pyray as rl

from openpilot.selfdrive.ui.sunnypilot.onroad.hud import tokens as T

MPS_TO_MPH = 2.23694

# v3.5.2: 400 m, matching scc_map_v2's own lookahead exactly. The strip is now
# full screen height, so the extra range costs nothing and the map shows
# precisely the horizon SCC-M reasons over — no more, no less.
RANGE_M = 400.0        # how far up the strip the route runs
# v3.5.6 — 90 m was almost exactly the space below the ego marker, so the tail
# ended right at the bottom edge with nothing to spare. Two things then ate into
# it: the filter runs at POLL_S with the pose OF THAT INSTANT, so between polls
# the tail is trimmed from a position the car has since left, and the displayed
# pose lags the polled one by POSE_TAU. The result is a chunk of already-driven
# road vanishing while it is still on screen, once a second.
#
# THE REMOVAL MUST BE DONE BY THE EDGE FADE, WHICH KNOWS WHERE THE SCREEN IS --
# not by a distance filter, which does not. So the tail is now kept well past
# the visible span and `edge_fade` is the only thing that ends it. Decimation
# (see DECIMATE_M) makes the extra points nearly free: 170 m of tail is about
# twenty of them.
BEHIND_M = 170.0       # keep this much of the road already driven, to fade out
POLL_S = 1.0           # source data is 1 Hz; parsing faster buys nothing
WHY_REPEAT_S = 30.0    # how often an unchanged 'no route' reason repeats in the log
MAX_POINTS = 400       # hard bound on how much JSON we will walk
RING_M = (100.0, 200.0, 300.0)

# v3.5.2 — NO CONTAINER. The plate (62% scrim + hairline border) was a box on
# the road view; what makes a thin ribbon legible is contrast AT the ribbon, not
# a rectangle behind it. Each segment is stroked twice: a wider, dark, partly
# transparent pass, then the colour on top. That is the "transparent dark
# backdrop around the road" — it follows the road instead of framing it, and it
# costs one extra draw_line_ex per segment.
HALO_PX = 7.0
HALO_ALPHA = 0.55

# v3.5.6 route decimation. See the loop in _poll for why this is the biggest
# single win available on this screen.
DECIMATE_M = 8.0     # keep one point per 8 m of route (~16 px at this scale)
DECIMATE_V = 0.4     # ...unless the speed changes by this much, m/s

EGO_FROM_BOTTOM = 0.82   # where "you are here" sits, as a fraction of height

# v3.5.1 — THE JITTER, and why it is fixed here rather than by polling faster.
# The source (mapd's LastGPSPosition) updates at 1 Hz, so the ego pose the route
# is drawn relative to used to change in one step per second and the whole
# ribbon snapped with it. Polling faster cannot help: there is no new data to
# read. Instead the DISPLAYED pose eases toward the polled one every frame and
# the route is re-projected from raw lat/lon each frame. The map therefore lags
# the fix by ~POSE_TAU, which is the correct trade for an awareness widget --
# it is showing you a road, not a countdown.
POSE_TAU = 0.35        # s, displayed-pose time constant
POSE_SNAP_M = 120.0    # a jump bigger than this is a new fix, not motion: snap

# How far inside the box a segment has fully faded. Non-zero so nothing is ever
# drawn hard against the edge -- there is no scissor here (see render()).
FADE_PX = 26.0

# FunnyPilot v3.5.9 — OPACITY IS A PROPERTY OF SCREEN POSITION, NOT OF A
# SEGMENT. `edge_fade` answered "how close is this point to any edge", and the
# ribbon took `min()` of its two endpoints. On a highway mapd publishes very
# few points, so ONE segment can span the whole strip -- and the moment either
# end left the box the ENTIRE segment was dropped. That is the reported
# jumpiness, and on a straight enough road it is the whole ribbon vanishing.
#
# Now the strip has a fixed vertical opacity profile that the road slides
# through: full above, half at the car, zero at the bottom edge. Freeze the
# frame at any moment and the gradient is the same, which is what was asked
# for. Long segments are SUBDIVIDED (MAX_SEG_PX) so the gradient applies ALONG
# them and a partly-visible segment draws its visible part instead of nothing.
OPACITY_BP = (0.00, 0.55, EGO_FROM_BOTTOM, 1.00)   # fraction of strip height
OPACITY_V = (1.00, 1.00, 0.50, 0.00)
MAX_SEG_PX = 16.0     # subdivide anything longer than this
MAX_SUBDIV = 64       # bound: a single bad point cannot explode the draw count

# delta (mph under the expected speed) -> tint. Below DELTA_LO the road is
# simply the road and gets no colour at all.
#
# v3.5.6 — THE RAMP WAS CALIBRATED FOR A DROP NOBODY EVER MAKES. Reaching full
# red needed 25 mph under the expected speed, so an ordinary 8-10 mph corner sat
# at t = 0.2-0.3 and rendered as barely-tinted grey. Reported as "it is white
# 99% of the time", and it was: most of the ribbon is straight road at delta 0,
# and the corners that were not straight still had no colour to show.
#
# DELTA_HI is now 13 mph, which is a firm corner rather than an implausible one,
# and the first coloured stop sits at t = 0.15 so a 4 mph trim is already
# visibly amber. The neutral is also darkened: it is the ROAD, and it was
# competing with the white ego marker and the white text for attention.
DELTA_LO = 2.0
DELTA_HI = 13.0
_RAMP = ((0.00, (118, 134, 152)),
         (0.15, (255, 214, 112)),
         (0.45, (255, 158, 66)),
         (0.75, (255, 104, 58)),
         (1.00, (255, 68, 68)))

_M_PER_DEG = 111320.0


def ramp_color(delta_mph: float) -> tuple[int, int, int]:
  """Neutral -> amber -> orange -> red. Pure; unit-tested."""
  if not T.finite(delta_mph):
    return _RAMP[0][1]
  t = T.clamp((delta_mph - DELTA_LO) / (DELTA_HI - DELTA_LO), 0.0, 1.0)
  for i in range(1, len(_RAMP)):
    t1, c1 = _RAMP[i]
    if t <= t1:
      t0, c0 = _RAMP[i - 1]
      k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
      return tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
  return _RAMP[-1][1]


def corner_speed_at(lat: float, lon: float, corners) -> float:
  """The speed SCC-M v2 will hold at this point on the route, or 0.0.

  `corners` is what `read_corners_shm()` returned: (lat, lon, half_len, v,
  conf) per corner. A point belongs to a corner when it is within that corner's
  half-length of its apex, and where corners overlap the SLOWER one wins —
  the same min() the controller takes, applied to the same list, so the ribbon
  cannot show a corner the cap is not honouring.

  0.0 means "no corner here", which the caller draws as untinted road. That is
  a real answer, not a failure: most of any route is straight.
  """
  best = 0.0
  clat = math.cos(math.radians(lat)) if -90.0 < lat < 90.0 else 1.0
  # INDEXED, NOT UNPACKED, AND THAT IS THE WHOLE FIX (v3.6.3). This read
  # `for c_lat, c_lon, half, v, _conf in corners:` — exactly five names — while
  # v3.6.2 widened the fp_corners entry to six fields to carry `visits`. Every
  # frame with a corner ahead therefore raised
  #     ValueError: too many values to unpack (expected 5)
  # and `safe_draw` disabled the whole minimap FOR THE SESSION. It looked like
  # the map working and then dying a few seconds into a drive, because on
  # straight road the list is empty and the loop body never runs.
  #
  # Reading by index makes this consumer indifferent to fields added for other
  # readers, which is the property a positional wire format needs if it is
  # going to keep growing.
  for c in corners:
    if len(c) < 5:
      continue
    c_lat, c_lon, half, v = c[0], c[1], c[2], c[3]
    if v <= 0.0:
      continue
    d = math.hypot((c_lat - lat) * _M_PER_DEG, (c_lon - lon) * _M_PER_DEG * clat)
    if d <= max(half, 1.0):
      best = v if best <= 0.0 else min(best, v)
  return best


# v3.6.2 — the faintest a ring is ever drawn. A corner driven ONCE has, by
# construction, converged on nothing yet: there is no second pass for it to
# agree with, so its settled-confidence is ~0 and the ring would be invisible.
# But "we have been here" is worth showing on its own, and it is a DIFFERENT
# claim from "we know how fast to take it". The floor states the first without
# implying the second.
SEEN_MIN_ALPHA = 0.20


def learned_corners_from(corners) -> list[tuple[float, float, float]]:
  """(lat, lon, settled) for every corner we have actually DRIVEN, out of what
  read_corners_shm() returned — i.e. `visits >= 1`.

  THE FILTER IS ON VISITS, THE OPACITY IS ON `settled`, AND THAT SPLIT IS THE
  WHOLE POINT (v3.6.2). Whether to draw a ring at all is a question about
  EXISTENCE — have we been here — which only the visit count answers. How
  solid to draw it is a question about CONVERGENCE — has the answer stopped
  moving — which only `settled` answers. Filtering on `settled` instead would
  hide every corner during its first few passes, which are exactly the ones a
  driver watching the first drives needs to watch appear.

  Pulled out of _poll() so the filter DIRECTION is testable off-device.
  _poll() itself needs a live /dev/shm handle and nothing off-device can
  reach it, and this repo has shipped exactly this class of bug before
  (LearnStore.nearby's ahead_only flip, v3.6.2) with every other test still
  green.
  """
  return [(c[0], c[1], c[4]) for c in corners if len(c) > 5 and c[5] >= 1]


# ── v3.6.4: the tint is a PLAN, not a footprint ────────────────────────────
#
# Until now a corner painted its colour over its own extent and nothing else,
# so the ribbon said WHERE the bend is but nothing about what the car is doing
# about it. The approach — the part you actually feel, where the throttle comes
# off and then the brakes come on — was untinted road.
#
# Now HUE still comes from the corner's FULL drop (so a hard bend looks hard
# from a mile out) and OPACITY is how much of that drop has already happened at
# each point on the road:
#
#     alpha(s) = (expected - cap(s)) / (expected - v_corner)
#
# 0 means the speed there is the driver's set speed and this corner has no say;
# 1 means the cap has arrived at the corner's own speed. Going IN, `cap` is
# SCC-M v2's own `approach_cap` — the integrated decel budget the controller
# uses, so the ribbon darkens exactly as the car sheds speed. Coming OUT it is
# CurveSpeedCap's release ramp, so the fade back to road grey happens where
# authority genuinely returns to the set speed rather than stopping dead at the
# corner's exit.
#
# THE TWO ENDS ARE ASYMMETRIC BY CONSTRUCTION and that is information: entry
# uses a budget capped at 1.20 m/s^2, the exit uses 2.5 m/s^2, so the run-out
# is about half the length of the run-in — which is what "brake early,
# accelerate out" looks like drawn on a road.
#
# v3.6.5 — THE RUN-OUT NOW STARTS AT THE CORNER'S EXIT, NOT AT ITS APEX, and
# the whole extent in between is solid. That is not a drawing change; the
# CONTROLLER changed to match (corner_speed.corner_cap), because holding the
# corner speed across the arc and releasing from the exit is what the geometry
# actually says. The ribbon takes the SAME function the cap does, so the two
# cannot disagree about where authority comes back.


def corner_plan_at(s: float, corners_s, corner_cap):
    """(v_gov, cap) at arc position `s` along the route.

    `corners_s` is [(s_apex, half_len, v_target), ...] in the same arc frame.
    The governing corner is the one whose envelope is LOWEST here — the same
    min() the controller takes, so the ribbon cannot disagree with the cap.

    Entries are read POSITIONALLY and tolerate extra fields: this tuple has
    already grown once, and a consumer that spells out every name breaks the
    next time one is added (the v3.6.3 minimap outage).

    (0.0, 0.0) means no corner has any say at this point.
    """
    best, gov = float('inf'), 0.0
    for c in corners_s:
      if len(c) < 3:
        continue
      s_apex, half, v = c[0], c[1], c[2]
      if v <= 0.0:
        continue
      cap = corner_cap(v, s_apex - s, half)
      if cap < best:
        best, gov = cap, v
    if gov <= 0.0 or not T.finite(best):
      return 0.0, 0.0
    return gov, best


def plan_alpha(expected_mps: float, v_gov: float, cap_mps: float) -> float:
    """How much of this corner's slowdown has already happened here, 0..1.

    1.0 AT EVERY APEX BY CONSTRUCTION, and that is the property that makes
    this safe to use as opacity: at the corner the remaining distance is zero,
    so `cap` IS `v_gov` and the ratio is exactly 1. No bend can fade away, however
    far off it is — only its run-in and run-out fade.
    """
    if not (T.finite(expected_mps) and T.finite(v_gov) and T.finite(cap_mps)):
      return 0.0
    full = expected_mps - v_gov
    if v_gov <= 0.0 or full <= 0.01:
      return 0.0
    return T.clamp((expected_mps - cap_mps) / full, 0.0, 1.0)


def blend(neutral, colour, a: float):
    """Road grey -> the corner's colour, by alpha. Kept as a blend rather than
    a real alpha channel because the ribbon is stroked over the camera image:
    fading toward the ROAD keeps it legible, fading toward transparent would
    let whatever is underneath decide what the driver sees."""
    a = T.clamp(a, 0.0, 1.0)
    return tuple(int(neutral[i] + (colour[i] - neutral[i]) * a) for i in range(3))


def confidence_alpha(conf: float) -> int:
  """A learned corner's ring opacity, 0..255, from its SETTLED confidence.

  v3.6.2 — WHAT THIS NUMBER MEANS CHANGED, AND THAT WAS THE FIX. It used to be
  `confidence_for(visits)`, which saturates at three visits, so a corner still
  shifting its own answer by 15% on its third pass painted a fully solid ring.
  It is now `confidence_of(visits, drift)`: successive passes have to AGREE,
  not merely accumulate. A corner driven thousands of times whose speed never
  moves reaches 1.0; three passes that keep changing their mind do not.

  Floored at SEEN_MIN_ALPHA because every corner that reaches this function
  has been driven at least once (see learned_corners_from), and that fact
  deserves to be visible before anything has converged.

  Pulled out of render() so it is testable off-device — the same treatment
  tint_delta_mph got in v3.5.9, for the same reason: nothing inside render()
  is reachable without a GL context, so a mutation here would be invisible to
  the whole suite. Non-finite input reads as the FLOOR, never as fully
  confident — a garbage number must not paint a solid ring.
  """
  c = T.clamp(conf, 0.0, 1.0) if T.finite(conf) else 0.0
  return int(max(SEEN_MIN_ALPHA, c) * 255)


def tint_delta_mph(expected_mps: float, corner_mps: float) -> float:
  """How much slower than expected this bit of road will be taken, mph.

  `corner_mps <= 0` MEANS NO CORNER HERE, NOT A CORNER AT ZERO SPEED. Since
  v3.6.2 the tag on a route point is the speed SCC-M v2 chose for the corner
  covering it, and most of any route is straight, so most points carry nothing.
  Subtracting the missing one from the expected speed would paint every
  straight road full red — the loudest possible way to say nothing is
  happening.

  Pulled out of `render()` so it can be tested: the sentinel is one `and` in a
  conditional expression, and a mutation that dropped it survived the whole
  suite because nothing off-device can reach the draw path.
  """
  if not (T.finite(expected_mps) and T.finite(corner_mps)):
    return 0.0
  if expected_mps <= 0.0 or corner_mps <= 0.0:
    return 0.0
  return (expected_mps - corner_mps) * MPS_TO_MPH


def expected_speed_at(ref_mps: float, zone_limit_mps: float,
                      sla_ratio: float, sla_active: bool) -> float:
  """The speed we expect to be doing where a route point is. v3.5.2.

  `ref_mps` is what we hold now — the set speed when cruise is set, otherwise
  the current speed. If SLA is active it will have walked us onto the limit of
  whatever zone is in force AT THAT POINT (plus the driver's carried offset
  ratio) by the time we get there, so that becomes the ceiling.

  `min()`, never `max()`: SLA can only be one more thing lowering the ceiling,
  and a driver's +20% offset must not be able to raise the expected speed above
  a set speed they deliberately chose.
  """
  ref = float(ref_mps)
  if not T.finite(ref) or ref <= 0.0:
    return 0.0
  if sla_active and T.finite(zone_limit_mps) and zone_limit_mps > 0.0:
    ratio = float(sla_ratio) if T.finite(sla_ratio) else 0.0
    ref = min(ref, zone_limit_mps * (1.0 + max(-0.9, ratio)))
  return ref


def zone_change(pts):
  """(index, new_limit, old_limit) of the first speed-limit change ahead, else None.

  v3.5.6 — the ribbon already carries the zone limit in force at every point
  (stored raw since v3.5.2 so the tint can be recomputed per frame), and nothing
  was drawing it. A boundary is exactly the kind of thing this widget is for:
  it is ahead, it is on the road, and it changes what the car will do there.
  It reuses the SIGN'S OWN palette -- red for a slower zone, green for a faster
  one -- so the marker on the map and the halo on the sign are the same
  statement about the same event, rather than two colour languages.
  """
  base = 0.0
  for f, _r, _v, lim, *_ in pts:
    if f >= 0.0 and lim > 0.0:
      base = lim
      break
  if base <= 0.0:
    return None
  for i, (f, _r, _v, lim, *_) in enumerate(pts):
    if f > 0.0 and lim > 0.0 and abs(lim - base) > 0.3:
      return i, lim, base
  return None


def to_ego_frame(lat: float, lon: float, lat0: float, lon0: float, bearing_deg: float):
  """(forward, right) metres from ego, with ego heading = +forward.

  Equirectangular — good to centimetres over the few hundred metres this
  widget covers, and hundreds of times cheaper than haversine per point.
  """
  north = (lat - lat0) * _M_PER_DEG
  east = (lon - lon0) * _M_PER_DEG * math.cos(math.radians(lat0))
  b = math.radians(bearing_deg)
  fwd = north * math.cos(b) + east * math.sin(b)
  right = -north * math.sin(b) + east * math.cos(b)
  return fwd, right


def bearing_lerp(cur: float, target: float, a: float) -> float:
  """Ease a heading the SHORT way round. 359 -> 1 is two degrees, not 358."""
  d = (target - cur + 180.0) % 360.0 - 180.0
  return (cur + d * a) % 360.0


def screen_opacity(py: float, rect: rl.Rectangle) -> float:
  """Vertical opacity profile of the strip. See OPACITY_BP."""
  if rect.height <= 0:
    return 0.0
  t = (py - rect.y) / rect.height
  if t <= OPACITY_BP[0]:
    return OPACITY_V[0]
  for i in range(1, len(OPACITY_BP)):
    if t <= OPACITY_BP[i]:
      t0, t1 = OPACITY_BP[i - 1], OPACITY_BP[i]
      v0, v1 = OPACITY_V[i - 1], OPACITY_V[i]
      k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
      return v0 + (v1 - v0) * k
  return OPACITY_V[-1]


def side_fade(px: float, rect: rl.Rectangle) -> float:
  """Horizontal clip only. The vertical direction is owned by screen_opacity,
  so a point high in the strip is NOT dimmed for being near the top."""
  d = min(px - rect.x, rect.x + rect.width - px)
  return T.clamp(d / FADE_PX, 0.0, 1.0)


def edge_fade(px: float, py: float, rect: rl.Rectangle) -> float:
  """1.0 well inside the box, easing to 0 within FADE_PX of any edge.

  v3.5.1: replaces a hard `inside()` test that made the road already driven
  vanish the instant it crossed the boundary. There is no scissor available
  here (nesting one would un-clip every later widget -- see render()), so the
  fade reaching zero BEFORE the boundary is what keeps the ribbon inside its
  box without one.
  """
  d = min(px - rect.x, rect.x + rect.width - px,
          py - rect.y, rect.y + rect.height - py)
  return T.clamp(d / FADE_PX, 0.0, 1.0)


# v3.5.5 — LANE OFFSET AND THE GAP. Two reports, one root each.
#
# 1. "the position marker sits to the right of the road line". It does, and the
#    geometry is honest: mapd's route points are the OSM way, i.e. the road
#    CENTRELINE, while the GPS fix is the car — in the right-hand lane, a lane
#    half-width off it, plus whatever the fix is out by. The marker is drawn at
#    the projection origin, so the ribbon lands beside it.
#
#    This map is about the road AHEAD — its shape, and the speeds SCC-M reads
#    off it. Lane position is not information here, it is the one thing that
#    stops the widget reading as "this is the road I am on". So the ribbon is
#    SHIFTED LATERALLY to pass through the marker. Translation only: no
#    rotation, no per-point warping, so every curve and every distance is
#    untouched and only the constant offset goes away.
#
# 2. "the current segment disappears too soon, leaving a gap between the marker
#    and the road ahead". mapd publishes the route from its matched position
#    forward, so after a re-match the first point can be tens of metres ahead
#    and there is nothing to draw between us and it. The ribbon is stitched
#    back to the origin in that case.
#
# BOTH ARE BOUNDED, and the bound is the safety property: a bad match must be
# allowed to look wrong rather than be allowed to drag the whole ribbon
# somewhere it does not belong.
LANE_SHIFT_MAX_M = 12.0   # beyond this the match is wrong, not the lane
STITCH_MAX_M = 60.0       # further than this and a straight line would be fiction


def lateral_offset_at_ego(pts) -> float:
  """The route's lateral offset where we are, metres (+right).

  Interpolated at fwd == 0 between the two points that bracket us, so it moves
  CONTINUOUSLY as the route slides past — picking the nearest point instead
  would step every time the nearest index changed, which is the jitter v3.5.1
  went to some trouble to remove. Falls back to the closest point when the
  route does not bracket us (i.e. it begins ahead), and returns 0.0 — no shift
  at all — for anything it cannot trust.
  """
  if not pts or len(pts) < 1:
    return 0.0
  best = None
  for i in range(1, len(pts)):
    f0, r0 = pts[i - 1][0], pts[i - 1][1]
    f1, r1 = pts[i][0], pts[i][1]
    if (f0 <= 0.0 <= f1) or (f1 <= 0.0 <= f0):
      if f1 == f0:
        best = r0
      else:
        t = (0.0 - f0) / (f1 - f0)
        best = r0 + (r1 - r0) * t
      break
  if best is None:
    # no bracket: use whichever end of the route is nearest to us
    nearest = min(pts, key=lambda p: abs(p[0]))
    best = nearest[1]
  if not T.finite(best) or abs(best) > LANE_SHIFT_MAX_M:
    return 0.0
  return float(best)


def stitch_to_ego(pts):
  """Prepend a point at the origin when the route begins AHEAD of us.

  Only ever adds; never moves or drops a real point. Bounded by STITCH_MAX_M
  because past that a straight segment would be inventing road geometry, which
  is the one thing this widget must not do.
  """
  if not pts:
    return pts
  f0 = pts[0][0]
  if not T.finite(f0) or f0 <= 0.0 or f0 > STITCH_MAX_M:
    return pts
  # v3.6.4 — INDEXED AND ARITY-TOLERANT, for the reason corner_speed_at was
  # fixed in v3.6.3: this tuple has now grown twice, and a consumer that spells
  # out every field breaks silently the next time a field is added for someone
  # else. Everything after (fwd, right) is carried through untouched.
  return [(0.0, 0.0, *pts[0][2:]), *pts]


class RouteMap:
  def __init__(self):
    self._params = None
    self._tried_params = False
    self._last_poll = 0.0
    self._last_frame = 0.0
    # raw route in geodetic coords: list of (lat, lon, delta_mph). Kept raw so
    # the smoothed pose below can re-project it every frame.
    self._raw: list[tuple[float, float, float]] = []
    self._fix = None            # (lat, lon, bearing) as last polled
    self._pose = None           # (lat, lon, bearing) as displayed, eased
    self._raw_corners: list[tuple[float, float, float]] = []  # (lat, lon, conf), conf > 0 only
    self._approach_cap = None   # lazily bound corner_speed.approach_cap, or False
    self._learned = False
    self._have_fix = False
    self._why_last = ""
    self._why_at = 0.0

  # ── data ────────────────────────────────────────────────────────────────

  def _mem(self):
    """Lazy /dev/shm/params handle. Never touched at import or construction —
    offroad it may not exist, and this module ships in the offroad process."""
    if not self._tried_params:
      self._tried_params = True
      try:
        from openpilot.common.params import Params
        self._params = Params("/dev/shm/params")
      except Exception:
        self._params = None
    return self._params

  @staticmethod
  def _num(raw) -> float:
    if isinstance(raw, (int, float)):
      return float(raw)
    try:
      return float(json.loads(raw))
    except Exception:
      try:
        return float(raw)
      except Exception:
        return 0.0

  def _why(self, reason: str) -> None:
    """FunnyPilot v3.6.0 — say WHY the minimap is empty, in the log.

    An empty strip is indistinguishable from a broken widget, and the on-screen
    text that used to say so was noise the other 99% of the time. The reason is
    logged instead: once when it changes, and at most every WHY_REPEAT_S while
    it persists, so a genuinely dead mapd does not fill the log.

    grep the swaglog for "route_map:" to see the whole history of why the map
    had nothing to draw. cloudlog is imported lazily and inside a try — this
    module is imported by the process that also draws the OFFROAD screen, and
    nothing here may fail at import (see test_hud_imports).
    """
    now = time.monotonic()
    if reason == self._why_last and (now - self._why_at) < WHY_REPEAT_S:
      return
    self._why_last, self._why_at = reason, now
    try:
      from openpilot.common.swaglog import cloudlog
      cloudlog.warning(f"route_map: no route - {reason}")
    except Exception:
      pass

  def _poll(self, now: float) -> None:
    if now - self._last_poll < POLL_S:
      return
    self._last_poll = now

    mem = self._mem()
    if mem is None:
      self._raw, self._raw_corners, self._have_fix = [], [], False
      self._why("no /dev/shm/params handle (offroad, or params not yet up)")
      return

    try:
      pos_raw = None
      pos_raw = mem.get("LastGPSPosition")
      pos = json.loads(pos_raw) if pos_raw else None
      lat0 = float(pos["latitude"])
      lon0 = float(pos["longitude"])
      bearing = float(pos.get("bearing", 0.0))
    except Exception as e:
      self._raw, self._raw_corners, self._have_fix = [], [], False
      why = "no LastGPSPosition - waiting on a GPS fix" if not pos_raw else \
            f"LastGPSPosition unreadable: {type(e).__name__}"
      self._why(why)
      return
    self._have_fix = True
    self._fix = (lat0, lon0, bearing)

    try:
      raw = mem.get("MapTargetVelocities")
      points = json.loads(raw) if raw else []
      if not raw:
        self._why("MapTargetVelocities empty - mapd has not matched a route")
      elif not points:
        self._why("MapTargetVelocities parsed to nothing")
    except Exception as e:
      points = []
      self._why(f"MapTargetVelocities unreadable: {type(e).__name__}")

    try:
      limit_now = self._num(mem.get("MapSpeedLimit"))
    except Exception:
      limit_now = 0.0

    nxt_limit, nxt_fwd = 0.0, None
    try:
      raw = mem.get("NextMapSpeedLimit")
      section = raw if isinstance(raw, dict) else (json.loads(raw) if raw else None)
      if isinstance(section, dict):
        nxt_limit = float(section.get("speedlimit") or 0.0)
        nlat, nlon = section.get("latitude"), section.get("longitude")
        if nlat is not None and nlon is not None:
          nxt_fwd = to_ego_frame(float(nlat), float(nlon), lat0, lon0, bearing)[0]
    except Exception:
      nxt_limit, nxt_fwd = 0.0, None

    # The controller's own answers, read BEFORE the point loop because each
    # kept point is tagged with the corner speed that applies there.
    corners = []
    try:
      from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import (
        read_corners_shm, read_scc_shm)
      corners = read_corners_shm()
      *_, learned = read_scc_shm()
      self._learned = bool(learned)
    except Exception:
      self._learned = False

    # v3.6.4 — SCC-M v2's OWN approach envelope, imported lazily and cached.
    # corner_speed.py is stdlib-only, but the import runs at poll time rather
    # than at module scope for the same reason the Params handle does: nothing
    # in hud/ may fail at import, and this module ships in the process that
    # draws the offroad screen. If it is unavailable the ribbon simply keeps
    # the pre-v3.6.4 look (full opacity over each corner's own extent).
    if self._approach_cap is None:
      try:
        from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.corner_speed import corner_cap
        self._approach_cap = corner_cap
      except Exception:
        self._approach_cap = False

    # v3.6.2: which of these corners have we actually DRIVEN. Filtered here,
    # once per poll, rather than in render(): a corner's learned state does
    # not change frame to frame, and render() should not spend a
    # comprehension on every draw.
    self._raw_corners = learned_corners_from(corners)

    # v3.6.4: corner apexes in the SAME ego frame the points are walked in, so
    # the plan is evaluated along the road rather than as a straight line.
    corners_fwd = []
    _cl = math.cos(math.radians(lat0))
    for c in corners:
      try:
        if c[3] <= 0.0:
          continue
        _n = (c[0] - lat0) * _M_PER_DEG
        _e = (c[1] - lon0) * _M_PER_DEG * _cl
        # (s_apex, half_len, v_target). v3.6.5 carries the half-length so the
        # ribbon holds the corner's colour across its whole arc and fades from
        # the EXIT, which is what the cap now does.
        corners_fwd.append((_n * math.cos(math.radians(bearing)) +
                            _e * math.sin(math.radians(bearing)), c[2], c[3]))
      except Exception:
        continue

    pts = []
    seen = 0
    kept_at, kept_v, kept_lim = None, 0.0, 0.0
    # v3.5.6: the same trig hoist _project already does. to_ego_frame recomputes
    # cos(radians(lat0)) and the two bearing terms PER POINT, and all three are
    # loop-invariant -- 1600 transcendental calls per poll where 4 will do.
    _b = math.radians(bearing)
    _cb, _sb = math.cos(_b), math.sin(_b)
    _clat = math.cos(math.radians(lat0))
    for p in points[:MAX_POINTS]:
      try:
        plat, plon = float(p["latitude"]), float(p["longitude"])
        _n = (plat - lat0) * _M_PER_DEG
        _e = (plon - lon0) * _M_PER_DEG * _clat
        fwd = _n * _cb + _e * _sb
        right = -_n * _sb + _e * _cb
      except Exception:
        continue
      # Keep a little of the road already driven so it can FADE out of the box
      # rather than blink out of it.
      if fwd < -BEHIND_M or fwd > RANGE_M + 40.0:
        continue
      # v3.6.2 — THE SPEED TAG IS OURS, NOT mapd's. `p["velocity"]` is
      # deliberately not read: SCC-M v2 does not use it, so drawing it would
      # show the driver a corner speed nothing in the car has agreed to.
      # v3.6.4 — TWO NUMBERS PER POINT, BOTH GEOMETRY-ONLY so they survive the
      # 1 Hz poll: `v` is the governing corner's own speed (which sets the HUE)
      # and `cap` is the envelope value here (which sets the OPACITY). The live
      # set speed is applied per frame in render(), where it belongs.
      if self._approach_cap:
        v, cap = corner_plan_at(fwd, corners_fwd, self._approach_cap)
      else:
        v, cap = corner_speed_at(plat, plon, corners), 0.0
      # which zone is in force at this point. Stored RAW, not folded into a
      # delta: since v3.5.2 the comparison depends on the live set speed and
      # SLA offset, which change every frame while this poll is 1 Hz.
      seen += 1
      lim = nxt_limit if (nxt_fwd is not None and nxt_limit > 0 and fwd > nxt_fwd) else limit_now

      # v3.5.6 DECIMATION -- the single biggest cost on this screen. mapd
      # spaces its points about a metre apart; at this widget's ~2 px/m that
      # is a sub-pixel segment, and the ribbon was drawing ~800 of them twice
      # per frame. Keeping one point per DECIMATE_M gives a 16 px segment,
      # which is smooth at this size, for a sixteenth of the draw calls.
      # A point is kept regardless if its speed or zone differs from the last
      # kept one, so no colour transition is smeared -- decimation must lose
      # RESOLUTION, never INFORMATION.
      if kept_at is not None:
        if (abs(v - kept_v) < DECIMATE_V and lim == kept_lim
            and math.hypot(fwd - kept_at[0], right - kept_at[1]) < DECIMATE_M):
          continue
      kept_at, kept_v, kept_lim = (fwd, right), v, lim
      pts.append((plat, plon, v, lim, cap))

    if points and not pts:
      # the array had points but none survived the range filter -- the usual
      # cause is a stale route from before a re-match, i.e. the whole thing is
      # behind us or somewhere else entirely.
      self._why(f"all {len(points)} route points out of range, kept 0 of {seen}")
    self._raw = pts

  # ── pose smoothing ──────────────────────────────────────────────────────

  def _ease_pose(self, now: float) -> None:
    """Move the DISPLAYED pose toward the polled one. See POSE_TAU."""
    if self._fix is None:
      return
    if self._pose is None:
      self._pose = self._fix
      return

    dt = now - self._last_frame if self._last_frame else 1.0 / 60.0
    self._last_frame = now
    dt = T.clamp(dt, 0.0, 0.25)
    a = 1.0 - math.exp(-dt / POSE_TAU) if POSE_TAU > 0 else 1.0

    lat, lon, brg = self._pose
    tlat, tlon, tbrg = self._fix
    # A route change or a GPS relock moves us kilometres in one poll; easing
    # through that would drag the whole ribbon across the box for a second.
    jump = math.hypot((tlat - lat) * _M_PER_DEG,
                      (tlon - lon) * _M_PER_DEG * math.cos(math.radians(tlat)))
    if jump > POSE_SNAP_M:
      self._pose = self._fix
      return
    self._pose = (lat + (tlat - lat) * a, lon + (tlon - lon) * a,
                  bearing_lerp(brg, tbrg, a))

  # ── drawing ─────────────────────────────────────────────────────────────

  def _project(self):
    """Raw geodetic route -> ego-frame metres, using the EASED pose.

    Done every frame rather than at poll time -- that is the whole jitter fix.
    The trig is hoisted out of the loop, so a 400-point route costs a few
    thousand flops a frame and no transcendental calls at all.
    """
    if self._pose is None:
      return [], []
    lat0, lon0, brg = self._pose
    b = math.radians(brg)
    cb, sb = math.cos(b), math.sin(b)
    clat = math.cos(math.radians(lat0))

    def to_ego(plat, plon):
      north = (plat - lat0) * _M_PER_DEG
      east = (plon - lon0) * _M_PER_DEG * clat
      return north * cb + east * sb, -north * sb + east * cb

    pts = [(*to_ego(plat, plon), v, lim, cap) for plat, plon, v, lim, cap in self._raw]

    # v3.5.5: take the lane/centreline offset out so the ribbon runs through
    # the marker. Computed BEFORE the stitch (the stitch adds a point at the
    # origin, which would otherwise answer the question with its own input) and
    # applied to the governing point too, or the marker SCC-M chose would drift
    # off the road it belongs to.
    shift = lateral_offset_at_ego(pts)
    if shift:
      pts = [(f, r - shift, v, lim, cap) for f, r, v, lim, cap in pts]

    corners = [(*to_ego(plat, plon), conf) for plat, plon, conf in self._raw_corners]
    if shift:
      corners = [(f, r - shift, conf) for f, r, conf in corners]
    return stitch_to_ego(pts), corners

  def render(self, rect: rl.Rectangle, ref_mps: float = 0.0,
             sla_ratio: float = 0.0, sla_active: bool = False,
             metric: bool = False) -> None:
    now = time.monotonic()
    self._poll(now)
    self._ease_pose(now)
    pts, corners = self._project()

    # v3.5.2: NO PLATE. See HALO_PX — contrast is applied at the ribbon.
    cx = rect.x + rect.width / 2
    y0 = rect.y + rect.height * EGO_FROM_BOTTOM
    scale = (rect.height * EGO_FROM_BOTTOM - rect.height * 0.04) / RANGE_M

    def px(fwd: float, right: float):
      return cx + right * scale, y0 - fwd * scale

    # NO SCISSOR HERE, DELIBERATELY. AugmentedRoadView._render already has one
    # open around the whole content rect, and raylib's EndScissorMode simply
    # disables the test — it does not restore an outer region. Nesting one here
    # would silently un-clip every widget drawn after this on the frame.
    # v3.5.1: instead of dropping out-of-box segments (which made the road
    # already driven blink out of existence at the edge) the alpha eases to zero
    # over the last FADE_PX, so the ribbon leaves the strip by fading.
    y0b = rect.y + 2

    for r in RING_M:
      rr = r * scale
      if y0 - rr > y0b:
        rl.draw_ring(rl.Vector2(cx, y0), rr - 1.0, rr, 200, 340, 28, rl.Color(255, 255, 255, 20))

    # TWO PASSES over the whole ribbon, not two strokes per segment: drawing
    # halo-then-colour per segment would let the next segment's halo paint over
    # the previous segment's colour at every joint, which reads as a dashed
    # line. All the dark first, then all the colour.
    #
    # v3.5.6: the geometry and the colour are computed ONCE into `segs` and the
    # two passes then only draw. Before this, px(), edge_fade(),
    # expected_speed_at() and ramp_color() all ran twice per segment for a
    # result that cannot differ between passes. Identical output, half the work.
    segs = []
    for i in range(1, len(pts)):
      f0, r0, _v0, _l0, _c0 = pts[i - 1]
      f1, r1, v1, lim1, cap1 = pts[i]
      a, b = px(f0, r0), px(f1, r1)

      expected = expected_speed_at(ref_mps, lim1, sla_ratio, sla_active)
      # v3.6.4 — HUE from the corner's FULL drop, OPACITY from how much of it
      # has happened here. The hue must not fade with the alpha, or a hard
      # bend would look gentle from a distance instead of merely distant.
      full = ramp_color(tint_delta_mph(expected, v1))
      c = blend(_RAMP[0][1], full, plan_alpha(expected, v1, cap1))

      # SUBDIVIDE. A highway segment can span the whole strip; drawing it as one
      # line meant one opacity for all of it and, worse, dropping the whole
      # thing the moment either end left the box.
      n = int(math.hypot(b[0] - a[0], b[1] - a[1]) / MAX_SEG_PX) + 1
      if n > MAX_SUBDIV:
        n = MAX_SUBDIV
      dx, dy, df = (b[0] - a[0]) / n, (b[1] - a[1]) / n, (f1 - f0) / n
      for k in range(n):
        pa = (a[0] + dx * k, a[1] + dy * k)
        pb = (a[0] + dx * (k + 1), a[1] + dy * (k + 1))
        # evaluated at the MIDPOINT: taking min() of the two ends would step
        # wherever a sub-segment straddles a breakpoint.
        my = (pa[1] + pb[1]) * 0.5
        mx = (pa[0] + pb[0]) * 0.5
        op = screen_opacity(my, rect) * side_fade(mx, rect)
        if op <= 0.0:
          continue
        fwd = f0 + df * (k + 0.5)
        depth = T.clamp(1.0 - (max(fwd, 0.0) / RANGE_M) * 0.72, 0.15, 1.0)
        w = max(3.0, rect.width * 0.075 * (1.0 - T.clamp(fwd / RANGE_M, 0.0, 1.0) * 0.45))
        segs.append((pa, pb, w, int(HALO_ALPHA * op * 255),
                     c[0], c[1], c[2], int(depth * op * 255)))

    halo_w = HALO_PX * 2
    for a, b, w, ha, _cr, _cg, _cb, _ca in segs:
      rl.draw_line_ex(a, b, w + halo_w, rl.Color(0, 0, 0, ha))
    for a, b, w, _ha, cr, cg, cb, ca in segs:
      rl.draw_line_ex(a, b, w, rl.Color(cr, cg, cb, ca))

    # v3.5.6 zone boundary. A gate across the ribbon plus the new number, in the
    # sign's red/green. Drawn AFTER the ribbon so it reads as a marker on the
    # road, and before the governing point so a corner cap still wins the
    # foreground when the two land together.
    zc = zone_change(pts)
    if zc is not None:
      zi, new_lim, old_lim = zc
      zf, zr = pts[zi][0], pts[zi][1]
      g = px(zf, zr)
      if edge_fade(g[0], g[1], rect) >= 1.0:
        col = T.HALT if new_lim < old_lim else T.ENGAGED
        half = rect.width * 0.16
        rl.draw_line_ex((g[0] - half, g[1] + 1), (g[0] + half, g[1] + 1), 7.0,
                        rl.Color(0, 0, 0, 170))
        rl.draw_line_ex((g[0] - half, g[1]), (g[0] + half, g[1]), 3.0, col)
        conv = 3.6 if metric else MPS_TO_MPH
        T.text_centered_shadowed(T.font_bold(), str(round(new_lim * conv)),
                                 g[0], g[1] - 30, T.SZ_MICRO, col, 2.0)

    # v3.6.2 — one ring per corner we have actually DRIVEN, centred on that
    # corner's own apex, not on whichever point currently governs. Opacity is
    # the corner's SETTLED confidence as read off /dev/shm/fp_corners — the
    # same weight the controller itself gives the learned value in
    # `effective_a_lat`, so the ring and the car cannot disagree. A corner
    # with no visits was filtered out in _poll(): nothing is drawn for a
    # corner priced by geometry alone, because it has no "how sure are we".
    rad = rect.width * 0.075
    for cf, cr, conf in corners:
      g = px(cf, cr)
      if edge_fade(g[0], g[1], rect) >= 1.0:
        rl.draw_ring(rl.Vector2(g[0], g[1]), rad - 3.0, rad, 0, 360, 24,
                     rl.Color(255, 255, 255, confidence_alpha(conf)))

    # Text now carries its own shadow: with the plate gone there is nothing
    # behind it but the road.
    # v3.6.2: the governing corner is one we have driven before, so its speed
    # is experience rather than a guess from the road's shape.
    if self._learned:
      T.text_shadowed(T.font_bold(), "LRN", rect.x + 12, rect.y + 10, T.SZ_MICRO,
                      rl.Color(0x6E, 0xD2, 0xA8, 230), 1.6)

    if not self._have_fix:
      T.text_centered_shadowed(T.font_med(), "NO FIX", cx, rect.y + rect.height / 2 - 14,
                               T.SZ_MICRO, T.FAINT, 2.0)

    # v3.5.9: the marker marks a position ON A ROAD. With no route parsed --
    # no fix yet, or mapd still loading after a reboot -- a lone triangle
    # floating in an empty strip says nothing and reads as a broken widget.
    if pts:
      self._draw_ego(cx, y0, rect.width)

  @staticmethod
  def _draw_ego(cx: float, y0: float, width: float) -> None:
    """"YOU ARE HERE". v3.5.2 — the old bare white triangle did not read as the
    car's position, because nothing distinguished it from the route itself. It
    is now a dark-haloed disc AT the exact ego origin with a heading wedge above
    it: the disc says where, the wedge says which way, and the halo separates
    both from whatever the ribbon is doing underneath.

    draw_triangle_fan takes plain (x, y) tuples in this repo (see
    onroad/model_renderer.py's lead chevrons) — the proven call shape, and a fan
    sidesteps draw_triangle's winding-order sensitivity, which silently draws
    nothing when wrong.
    """
    r = max(7.0, width * 0.055)
    t = r * 1.5
    # heading wedge, dark pass then white
    rl.draw_triangle_fan([(cx, y0 - t * 1.55), (cx + t * 0.80, y0 - t * 0.10),
                          (cx - t * 0.80, y0 - t * 0.10)], 3, rl.Color(0, 0, 0, 165))
    rl.draw_triangle_fan([(cx, y0 - t * 1.30), (cx + t * 0.62, y0 - t * 0.18),
                          (cx - t * 0.62, y0 - t * 0.18)], 3, T.WHITE)
    # the disc marks the origin the whole projection is built on
    rl.draw_circle(int(cx), int(y0), r + 4.0, rl.Color(0, 0, 0, 175))
    rl.draw_circle(int(cx), int(y0), r, T.WHITE)
    rl.draw_circle(int(cx), int(y0), r * 0.42, rl.Color(0x0B, 0x0F, 0x14, 255))
