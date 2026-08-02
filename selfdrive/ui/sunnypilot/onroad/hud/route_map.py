"""FunnyPilot v3.5.0 — the route minimap: SCC-M's own input, drawn.

WHAT IT SHOWS, AND WHY THAT IS ALL IT SHOWS. The ribbon is
`MapTargetVelocities` — the matched route ahead, the identical array
`scc_map_v2._raw_cap_from_map()` reads. mapd publishes exactly eleven params
and NONE of them contain side roads: junctions, turnoffs and surrounding
geometry never leave the binary. Drawing them would have meant querying the
offline OSM database ourselves on the UI thread, and it would have shown the
driver geometry the controller cannot see — the opposite of a debug tool. So
the map is one road, which is also why it stays minimal.

THE TINT is the point, and v3.5.2 changed what it is measured against.

It used to be the POSTED LIMIT: `delta = limit_there - map_target_there`. That
answers "how much slower than the sign", which is not the question. A 35 mph
curve in a 55 zone glowed red even when your set speed was 45 — the map was
shouting about a 20 mph drop you were never going to take.

It is now measured against THE SPEED WE EXPECT TO BE DOING AT THAT POINT:

    expected = min(set speed, zone limit there x (1 + SLA offset) if SLA on)
    delta    = expected - map_target_velocity_there

Two things fall out of that, and both were asked for. Set speed 45 into a 35
curve is a 10 mph drop and reads amber, not red. And if SLA is going to have
taken 8 mph off you by the time you reach the bend — because the bend is in a
slower zone — the comparison already happens at the reduced speed, so the
colour shows the drop you will ACTUALLY feel, not the one from here.
See `expected_speed_at()`, which is pure and unit-tested.

THE MARKER is `argmin(v_allowed)`: the single point SCC-M is braking for. It
is NOT recomputed here. plannerd publishes it over /dev/shm/fp_scc (see
long_v2/scc_shm.py) precisely so two copies of a selection rule cannot drift —
a debug readout that disagrees with the controller is worse than none. Solid
means the fusion passed the map's cut through at full authority; hollow means
v3.4.9's corroboration is scaling it because the model has not seen the corner
yet. That distinction is the entire reason this widget exists.

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
BEHIND_M = 90.0        # keep this much of the road already driven, to fade out
POLL_S = 1.0           # source data is 1 Hz; parsing faster buys nothing
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

# delta (mph under the posted limit) -> tint. Below DELTA_LO the road is simply
# the road and gets no colour at all.
DELTA_LO = 3.0
DELTA_HI = 25.0
_RAMP = ((0.00, (143, 163, 184)),
         (0.30, (255, 209, 102)),
         (0.62, (255, 138, 61)),
         (1.00, (255, 77, 77)))

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
  _f, _r, v, lim = pts[0]
  return [(0.0, 0.0, v, lim), *pts]


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
    self._gov_ll = None         # (lat, lon, authority) or None
    self._advisory = False
    self._have_fix = False

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

  def _poll(self, now: float) -> None:
    if now - self._last_poll < POLL_S:
      return
    self._last_poll = now

    mem = self._mem()
    if mem is None:
      self._raw, self._gov_ll, self._have_fix = [], None, False
      return

    try:
      pos_raw = mem.get("LastGPSPosition")
      pos = json.loads(pos_raw) if pos_raw else None
      lat0 = float(pos["latitude"])
      lon0 = float(pos["longitude"])
      bearing = float(pos.get("bearing", 0.0))
    except Exception:
      self._raw, self._gov_ll, self._have_fix = [], None, False
      return
    self._have_fix = True
    self._fix = (lat0, lon0, bearing)

    try:
      raw = mem.get("MapTargetVelocities")
      points = json.loads(raw) if raw else []
    except Exception:
      points = []

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

    pts = []
    for p in points[:MAX_POINTS]:
      try:
        plat, plon = float(p["latitude"]), float(p["longitude"])
        fwd, _right = to_ego_frame(plat, plon, lat0, lon0, bearing)
      except Exception:
        continue
      # Keep a little of the road already driven so it can FADE out of the box
      # rather than blink out of it.
      if fwd < -BEHIND_M or fwd > RANGE_M + 40.0:
        continue
      try:
        v = float(p["velocity"])
      except Exception:
        continue
      # which zone is in force at this point. Stored RAW, not folded into a
      # delta: since v3.5.2 the comparison depends on the live set speed and
      # SLA offset, which change every frame while this poll is 1 Hz.
      lim = nxt_limit if (nxt_fwd is not None and nxt_limit > 0 and fwd > nxt_fwd) else limit_now
      pts.append((plat, plon, v, lim))
    self._raw = pts

    # the controller's own choice, not ours
    try:
      from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import read_scc_shm
      g_lat, g_lon, _gv, auth, adv = read_scc_shm()
      self._advisory = bool(adv)
      self._gov_ll = (g_lat, g_lon, auth) if (g_lat or g_lon) else None
    except Exception:
      self._gov_ll, self._advisory = None, False

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
      return [], None
    lat0, lon0, brg = self._pose
    b = math.radians(brg)
    cb, sb = math.cos(b), math.sin(b)
    clat = math.cos(math.radians(lat0))

    def to_ego(plat, plon):
      north = (plat - lat0) * _M_PER_DEG
      east = (plon - lon0) * _M_PER_DEG * clat
      return north * cb + east * sb, -north * sb + east * cb

    pts = [(*to_ego(plat, plon), v, lim) for plat, plon, v, lim in self._raw]

    # v3.5.5: take the lane/centreline offset out so the ribbon runs through
    # the marker. Computed BEFORE the stitch (the stitch adds a point at the
    # origin, which would otherwise answer the question with its own input) and
    # applied to the governing point too, or the marker SCC-M chose would drift
    # off the road it belongs to.
    shift = lateral_offset_at_ego(pts)
    if shift:
      pts = [(f, r - shift, v, lim) for f, r, v, lim in pts]

    gov = None
    if self._gov_ll is not None:
      gf, gr = to_ego(self._gov_ll[0], self._gov_ll[1])
      gov = (gf, gr - shift, self._gov_ll[2])
    return stitch_to_ego(pts), gov

  def render(self, rect: rl.Rectangle, ref_mps: float = 0.0,
             sla_ratio: float = 0.0, sla_active: bool = False) -> None:
    now = time.monotonic()
    self._poll(now)
    self._ease_pose(now)
    pts, gov = self._project()

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
    for _pass in (0, 1):
      for i in range(1, len(pts)):
        f0, r0, _v0, _l0 = pts[i - 1]
        f1, r1, v1, lim1 = pts[i]
        a, b = px(f0, r0), px(f1, r1)
        edge = min(edge_fade(a[0], a[1], rect), edge_fade(b[0], b[1], rect))
        if edge <= 0.0:
          continue
        depth = T.clamp(1.0 - (max(f1, 0.0) / RANGE_M) * 0.72, 0.15, 1.0)
        w = max(3.0, rect.width * 0.075 * (1.0 - T.clamp(f1 / RANGE_M, 0.0, 1.0) * 0.45))
        if _pass == 0:
          rl.draw_line_ex(a, b, w + HALO_PX * 2, rl.Color(0, 0, 0, int(HALO_ALPHA * edge * 255)))
        else:
          expected = expected_speed_at(ref_mps, lim1, sla_ratio, sla_active)
          delta = (expected - v1) * MPS_TO_MPH if expected > 0 else 0.0
          c = ramp_color(delta)
          rl.draw_line_ex(a, b, w, rl.Color(c[0], c[1], c[2], int(depth * edge * 255)))

    if gov is not None:
      gf, gr, auth = gov
      g = px(gf, gr)
      rad = rect.width * 0.075
      if edge_fade(g[0], g[1], rect) >= 1.0:
        if auth >= 0.99:
          rl.draw_ring(rl.Vector2(g[0], g[1]), rad - 3.0, rad, 0, 360, 24, T.WHITE)
          rl.draw_circle(int(g[0]), int(g[1]), rect.width * 0.021, T.WHITE)
        else:
          # hollow + dashed: the controller sees it, the fusion is scaling it back
          for a0 in (0, 90, 180, 270):
            rl.draw_ring(rl.Vector2(g[0], g[1]), rad - 3.0, rad, a0 + 12, a0 + 78, 10,
                         rl.Color(255, 255, 255, 210))

    # Text now carries its own shadow: with the plate gone there is nothing
    # behind it but the road.
    if self._advisory:
      T.text_shadowed(T.font_bold(), "ADV", rect.x + 12, rect.y + 10, T.SZ_MICRO,
                      rl.Color(0xFF, 0xB4, 0x54, 230), 1.6)

    if not self._have_fix:
      T.text_centered_shadowed(T.font_med(), "NO FIX", cx, rect.y + rect.height / 2 - 14,
                               T.SZ_MICRO, T.FAINT, 2.0)

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
