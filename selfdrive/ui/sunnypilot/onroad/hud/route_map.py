"""FunnyPilot v3.5.0 — the route minimap: SCC-M's own input, drawn.

WHAT IT SHOWS, AND WHY THAT IS ALL IT SHOWS. The ribbon is
`MapTargetVelocities` — the matched route ahead, the identical array
`scc_map_v2._raw_cap_from_map()` reads. mapd publishes exactly eleven params
and NONE of them contain side roads: junctions, turnoffs and surrounding
geometry never leave the binary. Drawing them would have meant querying the
offline OSM database ourselves on the UI thread, and it would have shown the
driver geometry the controller cannot see — the opposite of a debug tool. So
the map is one road, which is also why it stays minimal.

THE TINT is the point. Each segment is coloured by how far below the POSTED
LIMIT the map wants you to be there:

    delta = limit_in_force_there - map_target_velocity_there

so a 35 mph curve in a 35 zone stays neutral and a 35 mph curve in a 55 zone
glows. Neutral -> amber -> orange -> red across DELTA_LO..DELTA_HI mph. That
is the question you actually ask about SCC-M — not "how fast", but "how much
slower than it should be here".

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

RANGE_M = 300.0        # how far up the box the route runs
BEHIND_M = 60.0        # keep this much of the road already driven, to fade out
POLL_S = 1.0           # source data is 1 Hz; parsing faster buys nothing
MAX_POINTS = 400       # hard bound on how much JSON we will walk
RING_M = (100.0, 200.0)

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
      # which zone is in force at this point
      lim = nxt_limit if (nxt_fwd is not None and nxt_limit > 0 and fwd > nxt_fwd) else limit_now
      delta = (lim - v) * MPS_TO_MPH if lim > 0 else 0.0
      pts.append((plat, plon, delta))
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

    pts = [(*to_ego(plat, plon), d) for plat, plon, d in self._raw]
    gov = None
    if self._gov_ll is not None:
      gf, gr = to_ego(self._gov_ll[0], self._gov_ll[1])
      gov = (gf, gr, self._gov_ll[2])
    return pts, gov

  def render(self, rect: rl.Rectangle) -> None:
    now = time.monotonic()
    self._poll(now)
    self._ease_pose(now)
    pts, gov = self._project()

    T.plate(rect, 0.10)

    pad_b = rect.height * 0.11
    cx = rect.x + rect.width / 2
    y0 = rect.y + rect.height - pad_b
    scale = (rect.height - pad_b - rect.height * 0.06) / RANGE_M

    def px(fwd: float, right: float):
      return cx + right * scale, y0 - fwd * scale

    # NO SCISSOR HERE, DELIBERATELY. AugmentedRoadView._render already has one
    # open around the whole content rect, and raylib's EndScissorMode simply
    # disables the test — it does not restore an outer region. Nesting one here
    # would silently un-clip every widget drawn after this on the frame.
    # v3.5.1: instead of dropping out-of-box segments (which made the road
    # already driven blink out of existence at the bottom edge) the alpha eases
    # to zero over the last FADE_PX, so the ribbon leaves the box by fading.
    y0b = rect.y + 2

    for r in RING_M:
      rr = r * scale
      if y0 - rr > y0b:
        rl.draw_ring(rl.Vector2(cx, y0), rr - 1.0, rr, 200, 340, 28, rl.Color(255, 255, 255, 22))

    for i in range(1, len(pts)):
      f0, r0, _ = pts[i - 1]
      f1, r1, d1 = pts[i]
      a, b = px(f0, r0), px(f1, r1)
      edge = min(edge_fade(a[0], a[1], rect), edge_fade(b[0], b[1], rect))
      if edge <= 0.0:
        continue
      c = ramp_color(d1)
      depth = T.clamp(1.0 - (max(f1, 0.0) / RANGE_M) * 0.72, 0.15, 1.0)
      w = max(3.0, rect.width * 0.085 * (1.0 - T.clamp(f1 / RANGE_M, 0.0, 1.0) * 0.45))
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

    if self._advisory:
      T.text_at(T.font_bold(), "ADV", rect.x + 12, rect.y + 10, T.SZ_MICRO,
                rl.Color(0xFF, 0xB4, 0x54, 220), 1.6)

    if not self._have_fix:
      T.text_centered(T.font_med(), "NO FIX", cx, rect.y + rect.height / 2 - 14,
                      T.SZ_MICRO, T.FAINT, 2.0)

    # ego marker. draw_triangle_fan takes plain (x, y) tuples in this repo (see
    # onroad/model_renderer.py's lead chevrons) — matching the proven call shape
    # rather than guessing at pyray's Vector2 coercion. A fan also sidesteps
    # draw_triangle's winding-order sensitivity, which silently draws nothing.
    t = rect.width * 0.05
    rl.draw_triangle_fan([(cx, y0 - t * 1.15),
                          (cx + t * 0.72, y0 + t * 0.62),
                          (cx - t * 0.72, y0 + t * 0.62)], 3, T.WHITE)
