"""FunnyPilot v3.6.2 — SCC-M v2: our own corner speeds, measured and learned.

WHAT WAS REMOVED, AND WHY. Every version up to v3.6.1 took its corner speeds
from `MapTargetVelocities[].velocity` — a number mapd computes from OSM
geometry with someone else's assumptions about grip, comfort and margin. That
one input is the reason SCC-M has needed a vision veto, an advisory-limit
floor, a proximity authority and a speed trim: four mechanisms, all of them
compensating for a speed we did not choose and could not check.

SCC-M v2 chooses it. The pipeline is three files and one equation:

    road_geometry   the polyline the minimap draws  ->  corner radius R
    scc_learn_store what this car has demonstrated  ->  lateral budget a_lat
    corner_speed    v = sqrt(a_lat * R), then the approach envelope

Map data now does exactly two jobs on this fork: it draws the minimap, and it
carries speed limits for SLA. Its `velocity` field is not read by anything.

────────────────────────────────────────────────────────────────────────────
THE TWO HALVES, AND THE ORDER THEY RUN IN

`observe_frame()` runs at the carState rate and watches the car traverse the
corners the geometry found. `update()` runs at the model rate and turns the
corners ahead into a cap. Learning happens for corners BEHIND us and capping
for corners AHEAD, so a corner can never be capped from the pass that is
recording it — the same invariant v3.5.0 established, now guaranteed by
geometry rather than by call order.

LEARNING DOES NOT DEPEND ON ANYTHING BEING ENGAGED. The measurement is lateral
acceleration from the steering angle and steering behaviour, both of which
exist with openpilot off, so ordinary driving builds the map. It is also not
gated on the SmartCruiseControlMap toggle: turning SCC-M off should stop the
car slowing down, not stop it noticing things.

────────────────────────────────────────────────────────────────────────────
WHAT IT MAY NEVER DO

  * exceed the set speed. `output_v_target` is clamped to `v_cruise` at the
    end of `update()`. The governor's min() already guarantees this, but a
    property this important should hold locally where it can be tested, not
    only as a consequence of what a caller happens to do with the value.
  * command an acceleration. This is a speed-domain governor exactly like
    SCC-V; the MPC and the shaper own the deceleration.
  * raise anything on a failure. Every read is wrapped and every failure path
    returns CAP_INACTIVE, which makes the governor's min() a no-op.

Import-light: numpy is not used; the geometry is stdlib. Params readers are
injectable so the whole controller constructs in a test.
"""
import json
import math
import time

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_speed as CS_
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import road_geometry as RG
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.corner_effort import CornerPass, LateralEffort
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE, CurveSpeedCap
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_learn_store import LearnStore

try:
  from openpilot.common.params import Params
  _mem_params = Params("/dev/shm/params")
except Exception:
  Params = None
  _mem_params = None

_DT = 0.05
_V_MIN_ACTIVE = 5.0
_PARAM_CHECK_FRAMES = 100
_GEOM_PERIOD_S = 0.5      # the route is 1 Hz data; re-deriving faster buys nothing

# How close to a stored record a measured corner has to be to be the same
# corner. Deliberately looser than the store's own MERGE_M so a lookup finds
# what a write would have merged into.
MATCH_M = 45.0
MATCH_BEARING_DEG = 55.0
# How far past a corner's exit it stays in the list. Long enough for the
# observer's pass to close on the far side, short enough that a corner cannot
# keep capping the car down the straight after it.
BEHIND_KEEP_M = 25.0
# Two refreshes half a second apart place the same physical corner at slightly
# different coordinates, because the ego pose they were projected from moved.
# This is how far apart they may be and still be the same corner.
SAME_CORNER_M = 30.0

# Flags on a learned record.
FLAG_SELF = 1        # SCC-M v2 was governing during the pass
FLAG_ENGAGED = 2     # lateral control was active during the pass
FLAG_BLINKER = 4     # a blinker was on (recorded, and the pass is discarded)

# plannerd already subscribes to whichever of these the device uses, so reading
# it costs no new subscription. Preferred over mapd's LastGPSPosition for
# LEARNING because it carries horizontalAccuracy — a record keyed on a position
# we are unsure of is worse than no record, since it caps the car where no
# corner is.
_GPS_SERVICES = ("gpsLocation", "gpsLocationExternal")
MAX_GPS_AGE_S = 3.0
MAX_GPS_ACC_M = 12.0


def read_gps(sm):
  """(lat, lon, bearing_deg, accuracy_m, ok). Never raises.

  AGE COMES FROM `sm.recv_time`, the only domain-safe source on this fork (the
  v3.4.5 post-mortem: `unixTimestampMillis` is a wall-clock epoch and
  `logMonoTime` is stamped from different clocks by Python and C++ publishers).
  `recv_time == 0.` means nothing has arrived yet, which is a reject, not an
  age of zero. A valid-but-STALE fix is the dangerous case and the reason this
  gate exists: nothing else rejects it, and it would file a corner wherever the
  car was when the signal died.
  """
  try:
    for s in _GPS_SERVICES:
      if s not in sm.services or not sm.valid.get(s, False):
        continue
      recv = sm.recv_time.get(s, 0.)
      if recv <= 0. or (time.monotonic() - recv) > MAX_GPS_AGE_S:
        continue
      m = sm[s]
      lat, lon = float(m.latitude), float(m.longitude)
      if not (lat or lon):
        continue
      return lat, lon, float(m.bearingDeg), float(m.horizontalAccuracy), True
  except Exception:
    pass
  return 0.0, 0.0, 0.0, 1e9, False


def _default_route_reader():
  """The route polyline as [(lat, lon), ...]. GEOMETRY ONLY — the `velocity`
  field of this array is deliberately not read anywhere in SCC-M v2."""
  if _mem_params is None:
    return []
  try:
    raw = _mem_params.get("MapTargetVelocities")
    pts = json.loads(raw) if raw else []
    return [(float(p["latitude"]), float(p["longitude"])) for p in pts[:RG.MAX_POINTS]]
  except Exception:
    return []


def _log(msg: str) -> None:
  try:
    from openpilot.common.swaglog import cloudlog
    cloudlog.warning(msg)
  except Exception:
    pass


class TrackedCorner:
  """A corner from the geometry, with whatever the store knows about it."""
  __slots__ = ("lat", "lon", "bearing", "radius", "half_len", "distance",
               "a_lat", "visits", "confidence", "v_target")

  def __init__(self, lat, lon, bearing, radius, half_len, distance,
               a_lat, visits, confidence, v_target):
    self.lat, self.lon, self.bearing = lat, lon, bearing
    self.radius, self.half_len, self.distance = radius, half_len, distance
    self.a_lat, self.visits, self.confidence = a_lat, visits, confidence
    self.v_target = v_target


class SCCMapV2:
  def __init__(self, params=None, route_reader=None, store=None):
    self.params = params if params is not None else (Params() if Params is not None else None)
    self._read_route = route_reader or _default_route_reader
    self._store = store
    self._store_failed = False

    self.frame = -1
    self.enabled = self._read_enabled_param()
    self.state = "INACTIVE"
    self.output_v_target = CAP_INACTIVE
    self.output_a_target = 0.0
    self.raw_v_target = CAP_INACTIVE
    self.is_enabled = False
    self.is_active = False
    self.gas_gating_active = False
    self.corner_radius_m = 0.0
    # the governing corner, for the minimap and scc_fusion
    self.gov_lat = 0.0
    self.gov_lon = 0.0
    self.gov_distance = 0.0
    self.gov_confidence = 0.0
    # everything the geometry found ahead, for the minimap's tint
    self.corners: list[TrackedCorner] = []
    self.learned_count = 0

    self._cap = CurveSpeedCap(_DT)
    self._effort = LateralEffort()
    self._pass = CornerPass()
    self._pass_key = None       # (lat, lon, bearing, radius) of the corner being driven
    self._geom_at = 0.0
    self._last_sample_t = 0.0

  # ── store ─────────────────────────────────────────────────────────────────

  def store(self):
    """Lazy, warmed on the first onroad frame. Constructing it at import or in
    __init__ would put disk IO on a path that also runs in tests and CI; leaving
    it to first USE would land the one blocking read on the frame where the cap
    first matters, i.e. on a car already doing 5 m/s. plannerd starts at
    ignition with the car stationary, so pulling it forward is free."""
    if self._store is None and not self._store_failed:
      try:
        self._store = LearnStore()
      except Exception:
        self._store_failed = True
    return self._store

  def _read_enabled_param(self) -> bool:
    if self.params is None:
      return True
    try:
      return bool(self.params.get_bool("SmartCruiseControlMap"))
    except Exception:
      return True

  def _lookup(self, lat: float, lon: float, bearing: float):
    """(a_lat, visits, confidence) for a measured corner. Default when unknown."""
    s = self.store()
    if s is None:
      return CS_.A_LAT_DEFAULT, 0, 0.0
    try:
      # ahead_only=False: the query point IS the corner, so the offset is zero
      # and an ahead-of-us test would reject the record it is looking for. See
      # LearnStore.nearby — this defaulted the other way and nothing learned was
      # ever used, with every unit test still green.
      near = s.nearby(lat, lon, bearing, MATCH_M, MATCH_BEARING_DEG, ahead_only=False)
    except Exception:
      return CS_.A_LAT_DEFAULT, 0, 0.0
    if not near:
      return CS_.A_LAT_DEFAULT, 0, 0.0
    _d, c = min(near, key=lambda dc: dc[0])
    return (CS_.effective_a_lat(c.a_lo, c.a_hi, c.n), int(c.n),
            CS_.confidence_for(c.n))

  # ── geometry ──────────────────────────────────────────────────────────────

  def _refresh_corners(self, now: float, lat: float, lon: float, bearing: float) -> None:
    """Re-measure the road ahead and price every corner on it.

    Throttled to _GEOM_PERIOD_S because the source is 1 Hz. The DISTANCES go
    stale between refreshes by up to a car-length or two, which the approach
    envelope absorbs — it is smooth in distance by construction, so a small
    error in d is a small error in the cap, never a step.

    CORNERS WE ARE INSIDE, AND THE ONE JUST BEHIND, ARE KEPT. Dropping
    everything with d <= 0 would end the cap at the apex — exactly where the
    car must not accelerate — and would close the observer's pass halfway
    through the bend it is measuring. `BEHIND_KEEP_M` past the exit is enough
    for the pass to close cleanly on the far side.
    """
    if now - self._geom_at < _GEOM_PERIOD_S:
      return
    self._geom_at = now

    corners, s_ego = RG.corners_from_route(self._read_route(), lat, lon, bearing)
    out = []
    for rc in corners:
      d = rc.s_apex - s_ego
      if d > CS_.MAX_LOOKAHEAD_M or d < -(rc.half_len + BEHIND_KEEP_M):
        continue
      # the corner's own position, back in geodetic coords, so it can be looked
      # up in the store and drawn on the map
      clat, clon = self._to_geodetic(rc.x, rc.y, lat, lon, bearing)
      cbrg = (bearing + rc.heading_rel) % 360.0
      a_lat, visits, conf = self._lookup(clat, clon, cbrg)
      v = max(CS_.speed_for(rc.radius, a_lat), CS_.MIN_V_TARGET)
      out.append(TrackedCorner(clat, clon, cbrg, rc.radius, rc.half_len, d,
                               a_lat, visits, conf, v))
    self.corners = out
    s = self.store()
    if s is not None:
      self.learned_count = s.count

  @staticmethod
  def _to_geodetic(fwd: float, right: float, lat0: float, lon0: float, bearing_deg: float):
    """Inverse of road_geometry.to_local, for one point."""
    b = math.radians(bearing_deg)
    north = fwd * math.cos(b) - right * math.sin(b)
    east = fwd * math.sin(b) + right * math.cos(b)
    clat = math.cos(math.radians(lat0)) or 1e-6
    return lat0 + north / RG.M_PER_DEG, lon0 + east / (RG.M_PER_DEG * clat)

  @staticmethod
  def _sep_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    return math.hypot((a_lat - b_lat) * RG.M_PER_DEG,
                      (a_lon - b_lon) * RG.M_PER_DEG * math.cos(math.radians(a_lat)))

  # ── learning ──────────────────────────────────────────────────────────────

  def observe_frame(self, now: float, v_ego: float, curvature: float,
                    steering_angle_deg: float, steer_torque: float,
                    lat_active: bool, saturated: bool, eps_limited: bool,
                    blinker: bool, standstill: bool, gps_acc: float) -> None:
    """Watch the car drive. Called at the carState rate; never raises.

    A pass opens when the car enters the extent of the nearest corner ahead and
    closes when it leaves it. Because the extent comes from the geometry, this
    knows what a corner IS rather than inferring one from a speed dip — which
    is what lets it learn with nothing engaged, and what makes leads, lights
    and queues irrelevant instead of exclusions.
    """
    try:
      dt = now - self._last_sample_t if self._last_sample_t else 0.0
      self._last_sample_t = now
      if not (0.0 < dt < 0.25):
        # A GAP IN THE SAMPLES ABANDONS THE PASS. Anything from a stalled
        # plannerd to an ignition cycle lands here, and a traversal we did not
        # watch continuously is not a measurement of it — the peak lateral
        # acceleration and the reversal RATE both depend on having seen the
        # whole thing. Without this the pass simply stays open across the gap
        # and commits whatever it happened to hold, keyed to a corner the car
        # may be nowhere near.
        if self._pass.open:
          self._pass.reset()
          self._pass_key = None
        self._effort.reset()
        return

      self._effort.update(dt, v_ego, curvature, steering_angle_deg,
                          steer_torque, lat_active, saturated, eps_limited)

      inside = None
      for c in self.corners:
        if abs(c.distance) <= c.half_len + RG.RESAMPLE_M:
          inside = c
          break

      blocked = bool(blinker or standstill or gps_acc > MAX_GPS_ACC_M)
      if self._pass.open:
        self._pass.add(self._effort, dt, v_ego, blocked=blocked)
        # Still the SAME corner? Each refresh re-projects from a moved ego pose,
        # so the coordinates shift by metres between refreshes; matching on
        # separation rather than equality is what stops the pass being closed
        # and reopened twice a second in the middle of one bend.
        k = self._pass_key
        still_here = (inside is not None and k is not None
                      and self._sep_m(inside.lat, inside.lon, k[0], k[1]) <= SAME_CORNER_M)
        if not still_here:
          self._commit_pass(lat_active)
        return

      if inside is not None and v_ego >= _V_MIN_ACTIVE and gps_acc <= MAX_GPS_ACC_M:
        self._pass.begin()
        self._pass_key = (inside.lat, inside.lon, inside.bearing, inside.radius)
        self._pass.add(self._effort, dt, v_ego, blocked=blocked)
    except Exception:
      pass

  def _commit_pass(self, lat_active: bool) -> None:
    key, self._pass_key = self._pass_key, None
    try:
      if key is None or not self._pass.usable():
        return
      a_peak, severity = self._pass.verdict()
      s = self.store()
      if s is None:
        return
      flags = (FLAG_SELF if self.is_active else 0) | (FLAG_ENGAGED if lat_active else 0)
      # A PASS WE OURSELVES HELD BACK IS NOT EVIDENCE THAT THE CORNER IS FAST.
      # Without this the feature reinforces itself: the cap sets the speed, the
      # speed sets a_peak, a_peak raises the floor, and the estimate ratchets.
      # It blocks only the FLOOR — a governed pass that still oscillated is
      # real evidence in the safe direction, and refusing that would mean the
      # one case where we are demonstrably wrong is the one we never learn from.
      s.observe(key[0], key[1], key[2], key[3], a_peak, severity, flags,
                allow_raise=not self.is_active)
      self.learned_count = s.count
      _log(f"scc_map_v2: pass R={key[3]:.0f}m a_peak={a_peak:.2f} sev={severity:.2f}"
           + f" rev={self._pass.reversals} lim={self._pass.limit_time:.2f}s flags={flags}")
    except Exception:
      pass
    finally:
      self._pass.reset()

  def flush(self, now: float) -> None:
    try:
      s = self.store()
      if s is not None:
        s.maybe_flush(now)
    except Exception:
      pass

  # ── the cap ───────────────────────────────────────────────────────────────

  def _raw_cap(self, v_cruise: float) -> float:
    """min over corners of the approach envelope. CAP_INACTIVE when none bind."""
    self.gov_lat = self.gov_lon = 0.0
    self.gov_distance = 0.0
    self.gov_confidence = 0.0
    self.corner_radius_m = 0.0

    best, best_c = CAP_INACTIVE, None
    for c in self.corners:
      if c.v_target >= v_cruise - 0.5:
        continue          # this corner does not constrain us at this speed
      # A corner we are INSIDE has distance <= 0 and its cap is simply the
      # corner speed — clamping the distance at zero is what holds the car
      # down through the bend instead of releasing at the apex. Once the exit
      # passes, _refresh_corners drops it and CurveSpeedCap ramps back up.
      allowed = CS_.approach_cap(c.v_target, max(c.distance, 0.0))
      if allowed < best:
        best, best_c = allowed, c
    if best_c is not None:
      self.gov_lat, self.gov_lon = best_c.lat, best_c.lon
      self.gov_distance = best_c.distance
      self.gov_confidence = best_c.confidence
      self.corner_radius_m = best_c.radius
    return best

  def update(self, long_enabled: bool, v_ego: float, a_ego: float, v_cruise: float,
             lat: float, lon: float, bearing: float, gps_ok: bool) -> None:
    """NOTE the signature no longer takes `sm` or `fric`.

    `fric` is gone because there is nothing left for it to trim: it scaled
    mapd's suggested curve speeds, and there are no longer any. It was never a
    grip estimate in the first place — `liveParameters.frictionCoefficient` is
    a steering-model value — so multiplying our own budget by it would have
    been the same mistake with our number instead of someone else's.
    """
    self.frame += 1
    if self.frame % _PARAM_CHECK_FRAMES == 0:
      self.enabled = self._read_enabled_param()

    self.is_enabled = bool(long_enabled and self.enabled)
    # THE GEOMETRY IS REFRESHED WHETHER OR NOT THE FEATURE IS ON. The corner
    # list is what the observer uses to know where a bend is, and learning must
    # keep working with the toggle off — see the module docstring.
    if not gps_ok:
      self.corners = []
    else:
      try:
        self._refresh_corners(time.monotonic(), lat, lon, bearing)
      except Exception:
        self.corners = []

    if not self.is_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    try:
      self.raw_v_target = self._raw_cap(v_cruise)
    except Exception:
      self.raw_v_target = CAP_INACTIVE

    cap = self._cap.update(self.raw_v_target, v_ego, v_cruise)
    self.is_active = self._cap.active

    if self.is_active:
      self.state = "RELEASING" if self._cap.releasing else "ACTIVE"
      # THE SET SPEED IS A HARD CEILING ON THIS OUTPUT. The governor's min()
      # already enforces it, but a cap that can be read as "SCC-M wants 30 m/s"
      # when the driver asked for 20 is a value waiting to be misused, and
      # CurveSpeedCap's release ceiling deliberately sits a little above cruise.
      self.output_v_target = min(max(cap, CS_.MIN_V_TARGET), v_cruise)
      self.output_a_target = a_ego     # display only; the MPC owns decel
      self.gas_gating_active = v_ego > self.output_v_target + 0.5
    else:
      self.state = "INACTIVE"
      self.output_v_target = CAP_INACTIVE
      self.output_a_target = 0.0
      self.gas_gating_active = False

  def _reset(self):
    self.state = "INACTIVE"
    self.output_v_target = CAP_INACTIVE
    self.output_a_target = 0.0
    self.raw_v_target = CAP_INACTIVE
    self.is_active = False
    self.gas_gating_active = False
    self.corner_radius_m = 0.0
    self.gov_lat = 0.0
    self.gov_lon = 0.0
    self.gov_distance = 0.0
    self.gov_confidence = 0.0
    self._cap.reset()
