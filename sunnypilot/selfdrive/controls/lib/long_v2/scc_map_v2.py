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

LEARNING IS NOT GATED ON THE SmartCruiseControlMap TOGGLE — turning SCC-M off
should stop the car slowing down, not stop it noticing things. It IS gated on
LATERAL BEING ACTIVE (v3.6.4, corner_effort.MIN_ENGAGED_FRAC): a pass the
driver steered measures the driver, not the corner, and a tired or distracted
wide line would otherwise file a permanently slower bend on evidence that has
nothing to do with the road. See corner_effort's docstring — this reverses the
original requirement 4 deliberately.

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

# ── corners the geometry cannot see (v3.6.5) ───────────────────────────────
#
# THE STORE IS NOW A SOURCE OF CORNERS, NOT ONLY AN ANNOTATION ON THEM. Until
# v3.6.5 a TrackedCorner could only be created by `road_geometry`, and the
# store was consulted afterwards to price it. That has two consequences that
# were both reported:
#
#   * the documented short-sweep blind spot (v3.6.4: a 40 m radius through 20
#     degrees is 14 m of arc inside a 50 m window, read as R=172) means some
#     real bends are never in the list at all — so nothing caps for them, and
#     nothing can learn about them either, because the observer only opens a
#     pass INSIDE a listed corner.
#   * no ring has ever been seen on the minimap. The ring needs `visits >= 1`
#     on a listed corner, so a bend the geometry misses can never show one
#     however many times it is driven.
#
# A record in the store is a bend THIS CAR HAS MEASURED, which is stronger
# evidence that it exists than a polyline is. So records ahead of us are
# injected into the corner list directly, and geometry corners win where the
# two describe the same bend.
STORE_LOOKAHEAD_M = 400.0
STORE_DEDUPE_M = 60.0      # a store record this close to a geometry corner IS it
# How much of a bend the car has to be pulling before an undetected stretch of
# road is a candidate corner at all. 0.0035 1/m is R = 285 m, which at 30 m/s
# is 3.2 m/s^2 — well past anything a straight road produces.
ORPHAN_K_MIN = 0.0035
# ...and how far the curvature has to fall before the bend is over.
ORPHAN_K_END = 0.0020
ORPHAN_MIN_S = 1.0
ORPHAN_MAX_S = 25.0        # past this it is a road, not a corner

# ── corners we cannot manage (v3.6.5) ──────────────────────────────────────
#
# A corner whose learned interval has been driven to the floor and which STILL
# reports stressed passes is telling us something the cap cannot fix: slowing
# further is not available (A_LAT_MIN is the floor by construction) and has not
# worked. Ratcheting at the floor forever just makes the bend crawl.
#
# So it is flagged instead, and the driver is warned on the APPROACH rather
# than discovering it mid-bend. THE CAP IS NOT REMOVED — the owner's phrasing
# was "stop trying to slow down", and the part of that which is safe to do is
# stop DEMANDING MORE; taking an existing constraint off a bend the car has
# repeatedly failed is the one version of this that could hurt someone, so the
# floor cap stays and only the escalation and the silence end.
UNMANAGEABLE_A_LAT = CS_.A_LAT_MIN + 0.15
UNMANAGEABLE_VISITS = 2


def is_unmanageable(a_lo: float, a_hi: float, visits: int) -> bool:
  """Has this bend's learned interval bottomed out over more than one visit?

  A FUNCTION RATHER THAN TWO INLINE COPIES, and that is not tidiness: corners
  reach the list by two routes now (the route geometry and the store), each
  needed this test, and the first cut wrote it twice. A mutation to one copy
  then left the suite green through the other — which is the same shape as
  every duplicated rule this repo has been bitten by.

  BOTH TERMS ARE LOAD-BEARING. Without the floor test every learned corner
  raises a banner, which is how a warning becomes noise. Without the visit
  count a single catastrophic pass — which `seed` adopts OUTRIGHT — warns
  forever on one bad sample.
  """
  try:
    return bool(min(float(a_lo), float(a_hi)) <= UNMANAGEABLE_A_LAT
                and int(visits) >= UNMANAGEABLE_VISITS)
  except (TypeError, ValueError):
    return False
WARN_LEAD_T = 6.0          # s of travel before the entry that the warning appears
WARN_MIN_S = 3.0           # ...and the shortest time it stays up

# ── the gas gate ───────────────────────────────────────────────────────────
#
# WHAT IT IS FOR, and why it is not the same thing as the speed cap. The cap
# tells the MPC what speed to hold; the gate tells the planner to stop ADDING
# speed it is about to have to give back. A human lifts off well before they
# brake for a bend, and the difference between a car that does that and one
# that holds the throttle until the cap bites is most of what "smooth" means.
#
# IT IS ANTICIPATORY BY CONSTRUCTION. The gate asks the SAME envelope the cap
# uses, but at the distance we will be at in GATE_LEAD_T seconds: "if I keep
# this speed, will the cap be under me shortly?" If yes, coast. That makes the
# lead time an explicit number in seconds rather than something that falls out
# of where the envelope happens to cross.
# v3.6.5 — 3.0 -> 2.0. The gate is the COASTING phase, and coasting is what the
# owner asked for less of ("I'd rather apply a little bit of braking ... than
# slow down 50,000 feet before by gas gating"). It still lifts off before the
# cap bites — that is the whole reason it exists and a car that holds throttle
# into the envelope is the thing this replaced — but two seconds of lead rather
# than three, against a v3.6.5 envelope that itself starts later, moves most of
# the slowdown out of the coast and into a short firm decel. Corners gentle
# enough that coasting alone does it still never reach the brakes: the gate
# only clips the THROTTLE (accel_clip[1]), the floor is untouched, so a corner
# whose envelope never falls under the coast rate is a pure lift-off exactly as
# before.
GATE_LEAD_T = 2.0        # s of travel of lead the gate gets over the cap itself
# v_ego must actually be ABOVE the target. THE v3.4.8 POST-MORTEM IS ABOUT
# EXACTLY THIS: a gate defined on a command rather than on the state it is
# meant to protect fired when there was no throttle to cut, pinned the accel
# ceiling to the coast accel, and left the car unable to accelerate with no way
# out. A gate that cannot tell "we are too fast" from "a corner exists" is not
# a gate.
GATE_V_MARGIN = 0.5      # m/s
# HYSTERESIS, AND IT IS NOT COSMETIC. Without it the gate limit-cycles at its
# own threshold: gate off -> the car adds throttle -> it crosses the threshold
# -> gate on -> the car coasts -> it drops back under -> gate off. Measured in
# a closed-loop sim of an S-bend at about 1.5 s per cycle, which is squarely in
# the band a passenger feels as surging. The gate now needs the requirement to
# come back up to meet us before it lets go, not merely to stop being under us.
GATE_V_RELEASE = 0.0     # m/s of gap at which an ENGAGED gate releases
# A WATCHDOG, NOT A TUNING KNOB. The longest legitimate hold is one approach:
# 400 m at 27 m/s is about 15 s. Anything past this is a latch, and a latched
# throttle gate is the failure this fork has already shipped once.
GATE_MAX_S = 45.0

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


class TrackedCorner:
  """A corner from the geometry, with whatever the store knows about it."""
  __slots__ = ("lat", "lon", "bearing", "radius", "half_len", "distance",
               "a_lat", "visits", "confidence", "settled", "v_target",
               "sign", "turn_deg", "unmanageable")

  def __init__(self, lat, lon, bearing, radius, half_len, distance,
               a_lat, visits, confidence, v_target, sign=0, turn_deg=0.0,
               settled=0.0, unmanageable=False):
    self.lat, self.lon, self.bearing = lat, lon, bearing
    self.radius, self.half_len, self.distance = radius, half_len, distance
    # `confidence` is the fusion's "have we been here" (visit count only).
    # `settled` is "has the answer stopped moving" and is what the minimap
    # draws. See SCCMapV2._lookup for why these are deliberately two numbers.
    self.a_lat, self.visits, self.confidence = a_lat, visits, confidence
    self.settled = settled
    self.v_target = v_target
    # which way it turns, and how far through. An S-bend is two corners of
    # OPPOSITE sign, and telling that from one long corner is the difference
    # between holding speed through the middle and surging into the second half.
    self.sign, self.turn_deg = sign, turn_deg
    # v3.6.5 — this bend has driven its own budget to the floor and STILL
    # stresses the car. The cap cannot fix it; the driver is warned instead.
    self.unmanageable = unmanageable


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
    # The last committed pass, for the dev UI. A driver watching the first few
    # drives needs to see that learning HAPPENED, not just that a store exists —
    # an empty store and a store nothing is being written to look identical.
    self.last_pass = (0.0, 0.0, 0.0, 0.0)   # (a_peak, severity, radius, departure)
    self.pass_count = 0

    self._cap = CurveSpeedCap(_DT)
    self._effort = LateralEffort()
    self._pass = CornerPass()
    self._pass_key = None       # (lat, lon, bearing, radius) of the corner being driven
    self._geom_at = 0.0
    self._dr_at = 0.0        # v3.6.5: when the distances were last advanced
    self._orphan = None      # v3.6.5: a bend the geometry never listed
    self.orphan_count = 0
    self.corner_warning = False   # v3.6.5: an unmanageable bend is coming up
    self._warn_frames = 0
    self._last_sample_t = 0.0
    self._gate_frames = 0

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
    """(a_lat, visits, corroboration, settled) for a measured corner.

    v3.6.2 — TWO DIFFERENT QUESTIONS COME OUT OF HERE AND THEY MUST NOT BE THE
    SAME NUMBER, which is what they were until now:

      corroboration  "is this a real corner, independently of OSM?" — answered
                     by HAVING BEEN THERE. It floors scc_fusion's corroboration
                     and bypasses the vision-disagreement veto, and one
                     completed pass is the whole evidence it needs. Stays
                     `confidence_for(visits)`; the fusion's behaviour is
                     unchanged by this release.
      settled        "do we trust the NUMBER?" — answered by successive passes
                     agreeing. This is what weights the learned value in
                     `effective_a_lat` and what the minimap ring's opacity
                     shows.

    Conflating them is why a corner still moving 15% on its third pass counted
    as fully known. A corner can be certainly real and not yet worked out, and
    those two facts belong to different consumers.
    """
    s = self.store()
    if s is None:
      return CS_.A_LAT_DEFAULT, 0, 0.0, 0.0
    try:
      # ahead_only=False: the query point IS the corner, so the offset is zero
      # and an ahead-of-us test would reject the record it is looking for. See
      # LearnStore.nearby — this defaulted the other way and nothing learned was
      # ever used, with every unit test still green.
      near = s.nearby(lat, lon, bearing, MATCH_M, MATCH_BEARING_DEG, ahead_only=False)
    except Exception:
      return CS_.A_LAT_DEFAULT, 0, 0.0, 0.0
    if not near:
      return CS_.A_LAT_DEFAULT, 0, 0.0, 0.0
    _d, c = min(near, key=lambda dc: dc[0])
    return (CS_.effective_a_lat(c.a_lo, c.a_hi, c.n, drift=c.d), int(c.n),
            CS_.confidence_for(c.n), CS_.confidence_of(c.n, c.d))

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
    self._dr_at = now

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
      a_lat, visits, conf, settled = self._lookup(clat, clon, cbrg)
      v = max(CS_.speed_for(rc.radius, a_lat), CS_.MIN_V_TARGET)
      out.append(TrackedCorner(clat, clon, cbrg, rc.radius, rc.half_len, d,
                               a_lat, visits, conf, v, rc.sign, rc.turn_deg,
                               settled, self._unmanageable(clat, clon, cbrg)))
    out.extend(self._corners_from_store(lat, lon, bearing, out))
    self.corners = out
    s = self.store()
    if s is not None:
      self.learned_count = s.count

  def _unmanageable(self, lat: float, lon: float, bearing: float) -> bool:
    """Has this bend driven its own budget to the floor and still stressed us?

    v3.6.5 — the condition for warning rather than capping harder. Two terms,
    and both are needed: the learned interval has to have BOTTOMED OUT (there
    is no more speed to take away — A_LAT_MIN is the floor by construction),
    and it has to have done so over more than one visit, because a single
    catastrophic pass seeds the ceiling outright and one bad sample is not a
    pattern. False on any doubt: a warning nobody can act on is worse than no
    warning, and a store failure must not manufacture one.
    """
    try:
      s = self.store()
      if s is None:
        return False
      near = s.nearby(lat, lon, bearing, MATCH_M, MATCH_BEARING_DEG, ahead_only=False)
      if not near:
        return False
      _d, c = min(near, key=lambda dc: dc[0])
      return is_unmanageable(c.a_lo, c.a_hi, c.n)
    except Exception:
      return False

  def _update_warning(self, v_ego: float) -> None:
    """Is an unmanageable bend close enough to warn about? v3.6.5.

    THE LEAD TIME IS IN SECONDS, NOT METRES, so the warning arrives the same
    distance ahead in the driver's terms at any speed — and it is measured to
    the corner's ENTRY, not its apex, because being told about a bend once you
    are in it is the complaint this exists to fix.

    It LATCHES for WARN_MIN_S. Without that the banner would flicker as the
    geometry refresh moves the corner's distance across the threshold, and a
    warning that blinks reads as a glitch rather than as a warning.
    """
    if self._warn_frames > 0:
      self._warn_frames -= 1
    want = False
    try:
      if self.is_enabled and v_ego > 0.0:
        for c in self.corners:
          if not c.unmanageable:
            continue
          to_entry = c.distance - c.half_len
          if 0.0 <= to_entry <= v_ego * WARN_LEAD_T:
            want = True
            break
    except Exception:
      want = False
    if want:
      self._warn_frames = max(self._warn_frames, int(WARN_MIN_S / _DT))
    self.corner_warning = self._warn_frames > 0

  def _corners_from_store(self, lat, lon, bearing, geom):
    """Learned bends ahead that the geometry did not find. v3.6.5.

    A record in the store is a bend this car has MEASURED — position, heading,
    radius and lateral budget all from its own passes — which is better
    evidence that the bend exists than a polyline traced from imagery. The
    geometry's documented short-sweep blind spot means some real corners are
    never listed, and a corner that is never listed can neither cap the car nor
    accumulate another visit, so the miss is self-perpetuating.

    GEOMETRY WINS WHERE BOTH DESCRIBE THE SAME BEND. The polyline's apex is a
    live projection from the current pose, while a record's position is where
    the car was on some previous drive; deduping toward geometry keeps the
    fresher number and stops one bend being capped twice.

    Distance is straight-line along the heading, not arc length, so on a curvy
    road it UNDERSTATES how far away the corner is. That direction is
    deliberate: an understated distance tightens the cap early rather than
    arriving late. Never raises — a store failure yields no extra corners.
    """
    extra = []
    try:
      s = self.store()
      if s is None:
        return extra
      near = s.nearby(lat, lon, bearing, STORE_LOOKAHEAD_M,
                      MATCH_BEARING_DEG, ahead_only=True)
      cos_lat = math.cos(math.radians(lat))
      hx, hy = math.sin(math.radians(bearing)), math.cos(math.radians(bearing))
      for _d, c in near:
        if c.r <= 0.0 or c.n < 1:
          continue
        if any(self._sep_m(c.lat, c.lon, g.lat, g.lon) <= STORE_DEDUPE_M for g in geom):
          continue
        north = (c.lat - lat) * 111320.0
        east = (c.lon - lon) * 111320.0 * cos_lat
        d = north * hy + east * hx
        if not (0.0 < d <= CS_.MAX_LOOKAHEAD_M):
          continue
        a_lat = CS_.effective_a_lat(c.a_lo, c.a_hi, c.n, drift=c.d)
        # A record carries a radius but no sweep, so its extent is estimated
        # from the radius and capped. Erring short is the safe direction here:
        # it starts the run-out sooner, which hands throttle back rather than
        # holding the car down over road we have no evidence about.
        half = min(0.35 * float(c.r), 40.0)
        extra.append(TrackedCorner(
          c.lat, c.lon, c.bearing, c.r, half, d, a_lat, int(c.n),
          CS_.confidence_for(c.n), max(CS_.speed_for(c.r, a_lat), CS_.MIN_V_TARGET),
          0, 0.0, CS_.confidence_of(c.n, c.d),
          is_unmanageable(c.a_lo, c.a_hi, c.n)))
    except Exception:
      return []
    return extra

  def _dead_reckon(self, now: float, v_ego: float) -> None:
    """Close the corner distances by how far we have driven since the refresh.

    v3.6.5 — THE GEOMETRY IS THROTTLED TO _GEOM_PERIOD_S BUT THE CAP IS NOT,
    and nothing used to move `distance` in between. So the envelope was fed a
    distance that was correct at the refresh and then up to
    `v_ego * _GEOM_PERIOD_S` metres TOO LARGE by the end of the interval —
    13.4 m at 26.8 m/s, and ALWAYS in the loose direction, because the number
    only ever ages toward "the corner is further away than it is".

    What that is worth: on the steep part of the envelope the cap moves at
    a/cap = 1.2/17.7 = 0.068 m/s per metre, so 13.4 m of staleness is 0.9 m/s
    (2 mph) of extra speed carried into the bend — arriving as a 0.5 s
    STAIRCASE rather than a smooth descent, which is felt as well as measured.
    Together with the 5% headroom the MPC had over the envelope before this
    release, that on its own is enough to explain a corner entered too fast.

    Position still comes from the GPS at the refresh; this removes only the
    part of the error that is pure bookkeeping. A bad `dt` (clock jump, first
    frame, a stalled planner) is rejected rather than applied — over-closing
    the distance would tighten the cap on evidence we do not have.
    """
    dt = now - self._dr_at
    self._dr_at = now
    if not (0.0 < dt <= _GEOM_PERIOD_S) or not (0.0 <= v_ego < 100.0):
      return
    ds = v_ego * dt
    for c in self.corners:
      c.distance -= ds

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
                    blinker: bool, standstill: bool, gps_acc: float,
                    pitch_rate_deg_s: float = 0.0, departure_m: float = 0.0,
                    lane_change: bool = False, long_active: bool = True,
                    brake_pressed: bool = False, lead: bool = False,
                    gas_pressed: bool = False, lat: float = 0.0, lon: float = 0.0,
                    bearing: float = 0.0) -> None:
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
                          steer_torque, lat_active, saturated, eps_limited,
                          pitch_rate_deg_s, departure_m, lane_change,
                          long_active, brake_pressed, gas_pressed)

      inside = None
      for c in self.corners:
        if abs(c.distance) <= c.half_len + RG.RESAMPLE_M:
          inside = c
          break

      blocked = bool(blinker or standstill or gps_acc > MAX_GPS_ACC_M)
      if self._pass.open:
        self._pass.add(self._effort, dt, v_ego, blocked=blocked, lead=lead)
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
        self._pass.add(self._effort, dt, v_ego, blocked=blocked, lead=lead)
        return
      self._observe_orphan(dt, v_ego, curvature, lat, lon, bearing, gps_acc,
                           blocked, lead)
    except Exception:
      pass

  def _observe_orphan(self, dt, v_ego, curvature, lat, lon, bearing, gps_acc,
                      blocked, lead) -> None:
    """Watch a bend the geometry never told us about. v3.6.5.

    THE STRUCTURAL PROBLEM THIS CLOSES. A pass only opens INSIDE a listed
    corner, and the radius estimator has a documented blind spot on short-sweep
    bends. So a corner it misses is missed permanently: nothing caps for it,
    and — because no pass ever opens there — nothing ever learns that it exists
    either. The owner's report is the symptom: the car runs wide, the driver
    grabs it, and next time is identical.

    THE RADIUS COMES FROM THE CAR, NOT FROM THE MAP, and it is the better
    measurement of the two. `controlsState.curvature` is the vehicle model's
    reading of the steering angle, so `R = 1/|k|` at the tightest point is what
    the car actually drove — no node spacing, no smoothing window, no aliasing.
    That is why an orphan record is worth having even though its POSITION is
    only as good as the GPS.

    ONLY STRESSED ORPHANS ARE FILED, which is the owner's rule: a gentle
    unlisted bend cost nothing and needs no record, and filing every one would
    put the whole road network in a store sized for corners. A stressed one is
    exactly the case where the missing corner hurt.
    """
    if self._pass.open:
      return
    k = abs(float(curvature)) if curvature == curvature else 0.0
    if self._orphan is None:
      if (k < ORPHAN_K_MIN or v_ego < _V_MIN_ACTIVE or gps_acc > MAX_GPS_ACC_M
          or not (lat or lon)):
        return
      p = CornerPass()
      p.begin()
      self._orphan = {"pass": p, "k": k, "lat": lat, "lon": lon,
                      "brg": bearing, "t": 0.0}
    o = self._orphan
    o["t"] += dt
    o["pass"].add(self._effort, dt, v_ego, blocked=blocked, lead=lead)
    if k > o["k"]:
      # keep the TIGHTEST point, which is the apex and where a record belongs
      o["k"], o["lat"], o["lon"], o["brg"] = k, lat, lon, bearing
    if k >= ORPHAN_K_END and o["t"] <= ORPHAN_MAX_S:
      return
    self._commit_orphan()

  def _commit_orphan(self) -> None:
    o, self._orphan = self._orphan, None
    if o is None:
      return
    try:
      p = o["pass"]
      if o["t"] < ORPHAN_MIN_S or not p.usable():
        return
      a_peak, severity = p.verdict()
      # THE ONE EXTRA GATE AN ORPHAN HAS. A listed corner records every usable
      # pass because its existence is already established; an orphan's
      # existence is being ASSERTED by this record, so it has to have actually
      # hurt. Below the limit the bend was fine and needs no entry.
      if severity < 1.0:
        return
      s = self.store()
      if s is None:
        return
      radius = 1.0 / o["k"] if o["k"] > 0.0 else 0.0
      if radius <= 0.0:
        return
      s.observe(o["lat"], o["lon"], o["brg"], radius, a_peak, severity,
                FLAG_ENGAGED, allow_raise=False)
      self.learned_count = s.count
      self.orphan_count += 1
      self.last_pass = (a_peak, severity, radius, p.depart_peak)
      self.pass_count += 1
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
      # v3.6.5 — A DEMONSTRATED PASS IS ADOPTED, NOT EASED TOWARD. openpilot
      # steered the whole bend, nothing was stressed, and the driver held the
      # throttle: `a_peak` under those conditions is not an estimate of what the
      # corner supports, it is a demonstration that it supports it. `seed`
      # skips the ALPHA_FLOOR discount, which is what "learn quicker" means for
      # a floor. `allow_raise` still applies — a pass WE were governing is not
      # evidence about the corner however hard the driver pushed the pedal,
      # because the speed was ours. In practice the two coincide rarely, which
      # is why the gate reads as strict: with SCC-M holding the car down, gas
      # takes `long_active` false and the cap stops being ours a moment later.
      demo = self._pass.demonstrated()
      # v3.6.6 — A MANUAL LONGITUDINAL PASS IS RAISE-ONLY. See
      # scc_learn_store.observe: the driver's chosen speed can only ever argue
      # the corner is FASTER, never slower, because "I went round it at 35"
      # says nothing about a bend openpilot rates at 45. The lateral signals
      # measured during the same pass are unaffected and still lower the ceiling
      # on their own evidence — this only removes the SPEED from the argument.
      s.observe(key[0], key[1], key[2], key[3], a_peak, severity, flags,
                allow_raise=not self.is_active, seed=demo,
                allow_lower=not self._pass.long_manual)
      self.learned_count = s.count
      # v3.6.2 — NO LOG LINE HERE. The pass used to be written to the swaglog
      # so the thresholds could be calibrated from a drive. They are pinned by
      # deterministic tests instead, and the dev UI already carries `last_pass`
      # and `pass_count` live, which is the readout that actually gets looked
      # at. A cloudlog call on the commit path is one more thing that can
      # block inside a 100 Hz observer for nothing.
      self.last_pass = (a_peak, severity, key[3], self._pass.depart_peak)
      self.pass_count += 1
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
      allowed = CS_.corner_cap(c.v_target, c.distance, c.half_len)
      if allowed < best:
        best, best_c = allowed, c
    if best_c is not None:
      self.gov_lat, self.gov_lon = best_c.lat, best_c.lon
      self.gov_distance = best_c.distance
      self.gov_confidence = best_c.confidence
      self.corner_radius_m = best_c.radius
    return best

  def _update_gas_gate(self, v_ego: float, v_cruise: float) -> None:
    """Should the planner stop adding throttle? See the GATE_ constants.

    THREE NARROWINGS, ALL FAIL-SAFE — each can only make the gate fire in
    strictly fewer situations, which is the right direction for something whose
    failure mode is a car that will not accelerate:

      1. v_ego must be above the corner's requirement by GATE_V_MARGIN. With no
         speed to give back there is nothing to gate.
      2. a corner that permits more than the set speed is SKIPPED, using the
         same test `_raw_cap` uses. `min(v_target, v_cruise)` was the first
         draft of this and it is subtly wrong in both directions: it cannot
         stop such a corner gating (the envelope was already above v_ego), and
         when the driver is over the set speed on the pedal it makes a
         non-constraining sweeper gate. Skipping is what the cap does, so the
         gate and the cap now agree about which corners exist.
      3. GATE_MAX_S bounds any single hold. A latched throttle gate is the
         v3.4.8 failure and this is the backstop against it, not a knob.
    """
    gate = False
    if self.is_enabled and v_ego > 0.0:
      # a gate already engaged holds until the requirement comes back up to
      # meet us; see GATE_V_RELEASE
      margin = GATE_V_RELEASE if self.gas_gating_active else GATE_V_MARGIN
      for c in self.corners:
        if c.v_target >= v_cruise - 0.5:
          continue          # does not constrain us; same test the cap applies
        # where we will be in GATE_LEAD_T seconds, at the speed we hold now.
        # Through the same `corner_cap` the speed cap uses, so a corner already
        # behind us cannot gate the throttle off while its run-out is handing
        # authority back — the v3.6.5 exit fix would otherwise be undone by the
        # gate, which is a THROTTLE clip and would keep the car coasting.
        d = c.distance - v_ego * GATE_LEAD_T
        if CS_.corner_cap(c.v_target, d, c.half_len) < v_ego - margin:
          gate = True
          break

    self._gate_frames = self._gate_frames + 1 if gate else 0
    if self._gate_frames * _DT > GATE_MAX_S:
      gate = False
    self.gas_gating_active = gate

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
        now = time.monotonic()
        self._refresh_corners(now, lat, lon, bearing)
        # AFTER the refresh, which re-stamps `_dr_at`, so a frame that
        # refreshed advances nothing and a frame that did not advances exactly
        # its own dt. Without this the distances are a 0.5 s staircase that is
        # always loose — see _dead_reckon.
        self._dead_reckon(now, v_ego)
      except Exception:
        self.corners = []

    if not self.is_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    # BEFORE the cap, deliberately: the gate's whole job is to act while the
    # cap is still above us, so it is computed from the corner list rather than
    # from the cap's output.
    self._update_gas_gate(v_ego, v_cruise)
    self._update_warning(v_ego)

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
    else:
      self.state = "INACTIVE"
      self.output_v_target = CAP_INACTIVE
      self.output_a_target = 0.0

  def debug_row(self, authority: float = 0.0, scc_v: float = 0.0,
                corroboration: float = 0.0, stop_cap: float = 0.0,
                source: int = 0):
    """The dev-UI payload, in scc_shm's documented field order.

    v3.6.7 — the last four come from OUTSIDE SCC-M v2 (SCC-V's own cap and
    corroboration, the stop governor, and which constraint is binding) and are
    passed in rather than reached for. This object has no business knowing
    about the vision controller or the base planner, and one channel carrying
    the whole panel beats four channels the UI has to keep in sync.
    """
    gov = None
    for c in self.corners:
      if abs(c.lat - self.gov_lat) < 1e-7 and abs(c.lon - self.gov_lon) < 1e-7:
        gov = c
        break
    ahead = sum(1 for c in self.corners if c.distance > -5.0)
    cap = self.output_v_target if self.output_v_target < CAP_INACTIVE else 0.0
    return (ahead,
            gov.radius if gov else 0.0,
            gov.v_target if gov else 0.0,
            self.gov_distance,
            gov.a_lat if gov else 0.0,
            gov.visits if gov else 0,
            int(bool(self.gas_gating_active)),
            cap, authority, self.learned_count,
            self.last_pass[0], self.last_pass[1], self.last_pass[2], self.pass_count,
            self.last_pass[3], self.orphan_count,
            scc_v if scc_v < CAP_INACTIVE else 0.0, corroboration,
            stop_cap if stop_cap < CAP_INACTIVE else 0.0, int(source))

  def _reset(self):
    self.state = "INACTIVE"
    self.output_v_target = CAP_INACTIVE
    self.output_a_target = 0.0
    self.raw_v_target = CAP_INACTIVE
    self.is_active = False
    self.gas_gating_active = False
    self._gate_frames = 0
    self.corner_warning = False
    self._warn_frames = 0
    self.corner_radius_m = 0.0
    self.gov_lat = 0.0
    self.gov_lon = 0.0
    self.gov_distance = 0.0
    self.gov_confidence = 0.0
    self._cap.reset()
