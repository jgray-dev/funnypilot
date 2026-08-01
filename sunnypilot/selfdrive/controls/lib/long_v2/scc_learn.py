"""FunnyPilot v3.5.0 — SCC-Learn: the corner map this car builds by driving.

TWO HALVES, and the first one is where all the difficulty is.

────────────────────────────────────────────────────────────────────────────
1. CornerObserver — deciding what is worth remembering.

Recording "the car slowed down here" is easy and useless: the car slows for
traffic, lights, stop signs, junctions, speed-limit zones and lead vehicles,
and learning any of those would produce a system that brakes at a green light
forever because it once queued there. The observer only commits an
observation for a shape that is specific to a bend:

    a LOCAL MINIMUM IN SPEED, followed by RECOVERY, with nothing else
    explaining it.

The recovery requirement is the load-bearing part. A corner is transient — you
slow, you turn, you speed back up. A stop sign, a light, and congestion all
end in a stop or a long hold, so requiring the car to come back UP before the
observation is committed rejects them without needing to know they exist.

Everything else is exclusion, and each one names a thing that would otherwise
be learned as a corner:

    lead vehicle seen at any point  -> that was traffic
    v_min below MIN_CORNER_V        -> a stop, a junction, or congestion
    the posted limit changed        -> a speed-limit zone; SLA owns that
    SLA was ramping or gas-gating   -> ditto, and it is already handled
    standstill at any point         -> not a corner by definition
    dip longer than MAX_DIP_S       -> congestion, not geometry
    dip shorter than MIN_DIP_S      -> noise
    poor GPS accuracy               -> we do not know where we were

WHAT SPEED IS STORED: the minimum reached, plus LEARN_MARGIN. The observed
minimum already contains whatever margin the driver or SCC-V chose, so storing
it raw and then capping at it would compound the margin every visit. The store
itself then biases upward on repeat visits (see scc_learn_store.ALPHA_UP).

────────────────────────────────────────────────────────────────────────────
2. SCCLearnV1 — turning the map back into a cap.

Identical envelope maths to SCC-M (`sqrt(v^2 + 2*a*d_eff)`, arriving early by
ARRIVAL_LEAD_T) so a learned corner and a mapped corner produce the same SHAPE
of slowdown and the governor's min() compares like with like.

CONFIDENCE, and why one visit is not treated like ten. `confidence` rises with
visit count and is what scc_fusion turns into authority. A corner seen once
gets a real but partial cut; a corner seen three times or more gets the full
one. This is the honest reading of a single observation: it might have been a
squirrel.

SAFETY POSTURE. This is a speed-domain governor exactly like SCC-V and SCC-M —
it publishes a cap, the MPC and the shaper own the actual deceleration, and it
can never command an acceleration. Every read is wrapped: a corrupt store, a
missing GPS fix or a disk problem degrades to CAP_INACTIVE, which makes the
governor's min() a no-op.

Import-light (stdlib + long_v2 siblings).
"""
import math

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CurveSpeedCap, CAP_INACTIVE
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_learn_store import LearnStore

_DT = 0.05
_V_MIN_ACTIVE = 5.0
_MIN_V_TARGET = 5.0
_A_DECEL_APPROACH = 1.0    # m/s^2, same budget SCC-M uses
_ARRIVAL_LEAD_T = 2.0      # s — be at the corner speed this early
_MAX_LOOKAHEAD_M = 400.0

# ── observer ──────────────────────────────────────────────────────────────
MIN_CORNER_V = 8.0         # m/s (~18 mph). Below this it is a junction or a stop.
MIN_DROP_MS = 2.5          # m/s the speed must fall for a dip to count
MIN_DROP_FRAC = 0.10       # ...or this fraction of the entry speed, whichever is smaller
RECOVER_MS = 1.5           # m/s of recovery that closes a dip
MIN_DIP_S = 1.5            # shorter than this is noise
MAX_DIP_S = 25.0           # longer than this is congestion
MAX_GPS_ACC_M = 12.0       # horizontal accuracy we are willing to key a record on
LEARN_MARGIN = 1.05        # store slightly above the observed minimum

FLAG_VISION = 1            # SCC-V was active during the dip
FLAG_DRIVER = 2            # the driver was on the brake during the dip

# ── confidence ────────────────────────────────────────────────────────────
# One visit is real evidence but not proof; three is a pattern. scc_fusion
# turns this into how much of the requested cut actually reaches the governor.
CONF_FIRST = 0.45
CONF_STEP = 0.275
CONF_FULL_VISITS = 3


# plannerd already subscribes to whichever of these the device uses (see
# common/gps.get_gps_location_service), so reading it costs no new subscription.
# It is preferred over mapd's LastGPSPosition for LEARNING because it carries
# horizontalAccuracy — and a record keyed on a position we are not sure of is
# worse than no record, since it will cap the car somewhere a corner is not.
_GPS_SERVICES = ("gpsLocation", "gpsLocationExternal")


def read_gps(sm):
  """(lat, lon, bearing_deg, accuracy_m, ok). Never raises."""
  try:
    for s in _GPS_SERVICES:
      if s not in sm.services:
        continue
      if not sm.valid.get(s, False):
        continue
      m = sm[s]
      lat, lon = float(m.latitude), float(m.longitude)
      if not (lat or lon):
        continue
      return lat, lon, float(m.bearingDeg), float(m.horizontalAccuracy), True
  except Exception:
    pass
  return 0.0, 0.0, 0.0, 1e9, False


def confidence_for(visits: int) -> float:
  if visits <= 0:
    return 0.0
  return min(1.0, CONF_FIRST + CONF_STEP * (min(visits, CONF_FULL_VISITS) - 1))


class CornerObserver:
  """Watches for a speed dip that only a bend explains."""

  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self._active = False
    self._v_entry = 0.0
    self._v_min = 0.0
    self._t_start = 0.0
    self._lat = self._lon = self._bearing = 0.0
    self._flags = 0
    self._poisoned = False
    self._v_ref = 0.0
    # Read inside the dip branch. They MUST be set here and not only on the
    # entry edge: this object lives in plannerd, and an AttributeError there
    # is a dead planner, not a missed corner.
    self._limit_at_start = 0.0

  def update(self, t: float, v_ego: float, *, lat: float, lon: float, bearing: float,
             gps_acc: float, lead: bool, standstill: bool, speed_limit: float,
             sla_busy: bool, vision_active: bool, driver_braking: bool):
    """Returns (lat, lon, bearing, v_store, flags) when a dip commits, else None."""
    # slow-moving reference of "the speed we were holding"
    if v_ego > self._v_ref:
      self._v_ref = v_ego
    else:
      self._v_ref += (v_ego - self._v_ref) * 0.01

    drop_needed = min(MIN_DROP_MS, max(1.0, self._v_ref * MIN_DROP_FRAC))

    if not self._active:
      if (v_ego < self._v_ref - drop_needed and v_ego >= MIN_CORNER_V
          and not standstill and gps_acc <= MAX_GPS_ACC_M and (lat or lon)):
        self._active = True
        # Poison on the ENTRY frame too, not only inside the dip: a dip that
        # begins while a lead is present or SLA is ramping was never ours.
        self._poisoned = bool(lead or sla_busy)
        self._v_entry = self._v_ref
        self._v_min = v_ego
        self._t_start = t
        self._lat, self._lon, self._bearing = lat, lon, bearing
        self._flags = (FLAG_VISION if vision_active else 0) | (FLAG_DRIVER if driver_braking else 0)
        self._limit_at_start = speed_limit
      return None

    # ── inside a dip ──────────────────────────────────────────────────────
    self._poisoned = self._poisoned or lead or standstill or sla_busy
    if speed_limit > 0 and self._limit_at_start > 0 and abs(speed_limit - self._limit_at_start) > 0.5:
      self._poisoned = True    # a zone change, not a bend; SLA owns this
    if vision_active:
      self._flags |= FLAG_VISION
    if driver_braking:
      self._flags |= FLAG_DRIVER

    if v_ego < self._v_min:
      # the apex is where the record belongs, not where the dip started
      self._v_min = v_ego
      if gps_acc <= MAX_GPS_ACC_M and (lat or lon):
        self._lat, self._lon, self._bearing = lat, lon, bearing

    dur = t - self._t_start
    if dur > MAX_DIP_S:
      self.reset()
      return None

    if v_ego < self._v_min + RECOVER_MS:
      return None              # still down there

    # ── the dip closed: commit or discard ────────────────────────────────
    out = None
    ok = (not self._poisoned
          and self._v_min >= MIN_CORNER_V
          and dur >= MIN_DIP_S
          and (self._lat or self._lon))
    if ok:
      out = (self._lat, self._lon, self._bearing, self._v_min * LEARN_MARGIN, self._flags)

    lat_keep = self._v_ref
    self.reset()
    self._v_ref = lat_keep
    return out


class SCCLearnV1:
  """Speed-domain governor fed by the learned corner map."""

  def __init__(self, store=None, enabled: bool = True):
    self._store = store
    self._store_failed = False
    self.enabled = enabled
    self.is_enabled = False
    self.is_active = False
    self.output_v_target = CAP_INACTIVE
    self.raw_v_target = CAP_INACTIVE
    self.confidence = 0.0
    self.gov_lat = 0.0
    self.gov_lon = 0.0
    self.learned_count = 0
    self._cap = CurveSpeedCap(_DT)
    self._observer = CornerObserver()

  def store(self):
    """Lazy. Reading /data at import or construction would put disk IO on a
    path that also runs in test and CI environments; on the device it simply
    happens on the first onroad frame instead."""
    if self._store is None and not self._store_failed:
      try:
        self._store = LearnStore()
      except Exception:
        self._store_failed = True
    return self._store

  # ── learning ────────────────────────────────────────────────────────────

  def observe(self, t: float, v_ego: float, **kw) -> bool:
    """Feed the observer; persist anything it commits. Never raises."""
    try:
      out = self._observer.update(t, v_ego, **kw)
      if out is None:
        return False
      s = self.store()
      if s is None:
        return False
      lat, lon, bearing, v, flags = out
      s.observe(lat, lon, bearing, v, flags)
      self.learned_count = s.count
      return True
    except Exception:
      return False

  def flush(self, t: float) -> None:
    try:
      s = self.store()
      if s is not None:
        s.maybe_flush(t)
    except Exception:
      pass

  # ── the cap ─────────────────────────────────────────────────────────────

  def _raw_cap(self, lat: float, lon: float, bearing: float, v_cruise: float):
    """(cap, confidence). CAP_INACTIVE when nothing learned applies."""
    s = self.store()
    if s is None:
      return CAP_INACTIVE, 0.0
    self.learned_count = s.count
    best, best_conf = CAP_INACTIVE, 0.0
    self.gov_lat = self.gov_lon = 0.0
    for d, c in s.nearby(lat, lon, bearing, _MAX_LOOKAHEAD_M):
      v_c = max(c.v, _MIN_V_TARGET)
      if v_c >= v_cruise - 0.5:
        continue            # this corner does not constrain us at this speed
      d_eff = max(0.0, d - v_c * _ARRIVAL_LEAD_T)
      allowed = math.sqrt(v_c * v_c + 2.0 * _A_DECEL_APPROACH * d_eff)
      if allowed < best:
        best = allowed
        best_conf = confidence_for(c.n)
        self.gov_lat, self.gov_lon = c.lat, c.lon
    return best, best_conf

  def update(self, long_enabled: bool, v_ego: float, v_cruise: float,
             lat: float, lon: float, bearing: float, gps_ok: bool) -> None:
    self.is_enabled = bool(long_enabled and self.enabled)
    if not self.is_enabled or v_ego < _V_MIN_ACTIVE or not gps_ok:
      self._reset()
      return

    try:
      self.raw_v_target, self.confidence = self._raw_cap(lat, lon, bearing, v_cruise)
    except Exception:
      self.raw_v_target, self.confidence = CAP_INACTIVE, 0.0

    cap = self._cap.update(self.raw_v_target, v_ego, v_cruise)
    self.is_active = self._cap.active
    self.output_v_target = max(cap, _MIN_V_TARGET) if self.is_active else CAP_INACTIVE

  def _reset(self) -> None:
    self.is_active = False
    self.output_v_target = CAP_INACTIVE
    self.raw_v_target = CAP_INACTIVE
    self.confidence = 0.0
    self.gov_lat = self.gov_lon = 0.0
    self._cap.reset()
