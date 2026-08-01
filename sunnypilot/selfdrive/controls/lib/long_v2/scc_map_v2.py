"""FunnyPilot v3.2.6e — SCC-Map v2, rewritten to actually run.

The 3.2.5st version parsed MapTargetVelocities entries as {v, dist, radius},
but mapd writes [{latitude, longitude, velocity}, ...] — every lookup fell
through to defaults and SCC-M silently never activated.

This version parses the real format (same data the proven legacy M-TSC
used): locate ourselves on the route polyline (nearest point, forward
slice), then for every upcoming curve point j at distance d_j with curve
speed tv_j:

    v_curve_j   = max(MIN_V, tv_j * trim)          # bounded friction/comfort trim
    d_eff_j     = max(0, d_j - v_curve_j * LEAD_T)  # arrive at speed ~2 s early
    allowed now = sqrt(v_curve_j^2 + 2 * A_DECEL_APPROACH * d_eff_j)
    raw cap     = min_j(allowed now)

The constant-decel envelope replaces the old jerk-integral braking-point
math (which also had a broken quadratic root: `/ 2 * a` multiplied by a/2
instead of dividing by 2a). CurveSpeedCap adds debounce, no-step seeding
and rate-limited release, identical to SCC-V.

FunnyPilot v3.5.0 — ADVISORY LIMITS AS EVIDENCE, NOT AS A TARGET.

mapd publishes `MapAdvisoryLimit` / `NextMapAdvisoryLimit` (OSM
`maxspeed:advisory`) and nothing has ever read them. They are a different
KIND of signal from everything else here: `MapTargetVelocities` is geometry
that mapd computed, whereas an advisory limit is a speed a highway engineer
surveyed and signed. Where one exists it is the single best statement that a
corner or hazard is REAL — which is exactly the thing SCC-M has been unable
to establish on its own.

It is used in two bounded ways, and deliberately not as a target:

  1. AS A FLOOR ON THE CAP. Where geometry under-detects a corner the
     advisory can pull the cap down, but only to `advisory * ADVISORY_MARGIN`
     and never by more than ADVISORY_MAX_CUT off cruise. It can only ever
     LOWER the cap — `min()` is the only operator it touches.
  2. AS CORROBORATION. `advisory_active` is published for scc_fusion, which
     floors the map's authority when a posted advisory agrees that there is
     something here. A surveyed advisory speed does not stop being evidence
     because the model has not seen the bend yet.

WHY NOT A TARGET: advisory speeds are posted per-WAY, so a curvy road carries
one for its whole length. Obeying it literally would hold the car down
through straights between the bends. The geometry cap still decides the
SHAPE of the slowdown; the advisory only bounds how high that cap may sit and
how much authority it gets.

Speed-domain only; import-light (numpy + long_v2 siblings; params readers
are injectable for tests).
"""
import json
import math

import numpy as np

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CurveSpeedCap, CAP_INACTIVE

try:
  from openpilot.common.params import Params
  _mem_params = Params("/dev/shm/params")
except Exception:
  Params = None
  _mem_params = None

_DT = 0.05
_V_MIN_ACTIVE = 5.0
_MIN_V_TARGET = 3.0
_A_DECEL_APPROACH = 1.0   # m/s^2 comfortable approach decel budget
_ARRIVAL_LEAD_T = 2.0     # s — reach curve speed this early
_MAX_LOOKAHEAD_M = 400.0  # beyond this a curve cannot meaningfully constrain us
_FRIC_NOMINAL = 0.8
_FRIC_SCALE_MIN = 0.7
_FRIC_SCALE_MAX = 1.1
_PARAM_CHECK_FRAMES = 100

# v3.5.0 advisory limits. See the module docstring for why these are bounds and
# not a target.
_ADVISORY_MIN = 4.5        # m/s (~10 mph) — below this the tag is noise, ignore it
ADVISORY_MARGIN = 1.15     # advisory speeds are signed conservatively; allow 15% over
ADVISORY_MAX_CUT = 8.9     # m/s (~20 mph) — most an advisory alone may take off cruise

_R_EARTH = 6373000.0
_TO_RAD = math.pi / 180.0


def _haversine_m(lat_a, lon_a, lat_b, lon_b):
  """Vectorized great-circle distance in meters (inputs in degrees)."""
  ax, ay = lat_a * _TO_RAD, lon_a * _TO_RAD
  bx, by = np.asarray(lat_b) * _TO_RAD, np.asarray(lon_b) * _TO_RAD
  h = np.sin((bx - ax) / 2) ** 2 + np.cos(ax) * np.cos(bx) * np.sin((by - ay) / 2) ** 2
  return 2 * _R_EARTH * np.arctan2(np.sqrt(h), np.sqrt(1 - h))


def _default_position_reader():
  if _mem_params is None:
    return None
  try:
    raw = _mem_params.get("LastGPSPosition")
    if not raw:
      return None
    d = json.loads(raw)
    return float(d["latitude"]), float(d["longitude"])
  except Exception:
    return None


def _default_velocities_reader():
  if _mem_params is None:
    return []
  try:
    raw = _mem_params.get("MapTargetVelocities")
    return json.loads(raw) if raw else []
  except Exception:
    return []


def _as_obj(raw):
  """mem-params values arrive either already decoded or as a JSON string
  depending on the key's registered type. Accept both, never raise."""
  if raw is None or isinstance(raw, (int, float)):
    return raw
  if isinstance(raw, dict):
    return raw
  try:
    return json.loads(raw)
  except Exception:
    return None


def _default_advisory_reader():
  """(advisory_now, advisory_next, distance_to_next) in m/s and m.

  `MapAdvisoryLimit` mirrors `MapSpeedLimit` (a bare speed for the current
  way); `NextMapAdvisoryLimit` mirrors `NextMapSpeedLimit`
  ({speedlimit, latitude, longitude}). Both are best-effort: any parse
  problem returns "no advisory", which makes every consumer below a no-op.
  """
  if _mem_params is None:
    return 0.0, 0.0, 0.0
  try:
    now = _as_obj(_mem_params.get("MapAdvisoryLimit"))
    now = float(now) if isinstance(now, (int, float)) else 0.0
  except Exception:
    now = 0.0

  nxt, nxt_d = 0.0, 0.0
  try:
    section = _as_obj(_mem_params.get("NextMapAdvisoryLimit"))
    if isinstance(section, dict):
      nxt = float(section.get("speedlimit") or 0.0)
      lat, lon = section.get("latitude"), section.get("longitude")
      pos = _default_position_reader()
      if pos is not None and lat is not None and lon is not None:
        nxt_d = float(_haversine_m(pos[0], pos[1], [float(lat)], [float(lon)])[0])
  except Exception:
    nxt, nxt_d = 0.0, 0.0

  return now, nxt, nxt_d


def speed_trim(fric: float) -> float:
  """Bounded trim on mapd's curve speeds: comfort preference x a bounded
  slice of the friction estimate (a steering-model value, not road grip)."""
  tuning = get_tuning()
  scale = min(_FRIC_SCALE_MAX, max(_FRIC_SCALE_MIN, fric / _FRIC_NOMINAL))
  return float(np.clip(tuning.sccm_speed_trim * scale, 0.5, 1.1))


class SCCMapV2:
  def __init__(self, params=None, position_reader=None, velocities_reader=None, advisory_reader=None):
    self.params = params if params is not None else (Params() if Params is not None else None)
    self._read_position = position_reader or _default_position_reader
    self._read_velocities = velocities_reader or _default_velocities_reader
    self._read_advisory = advisory_reader or _default_advisory_reader
    # v3.5.0: True while a posted advisory limit is asking for a real reduction.
    # Read by scc_fusion as independent corroboration; see the module docstring.
    self.advisory_active = False
    self.advisory_v_target = CAP_INACTIVE
    # lat/lon of the point argmin(v_allowed) selected; 0,0 = none
    self.gov_lat = 0.0
    self.gov_lon = 0.0
    self.frame = -1
    self.enabled = self._read_enabled_param()
    self.state = "INACTIVE"  # INACTIVE / ACTIVE / RELEASING
    self.output_v_target = CAP_INACTIVE
    self.output_a_target = 0.0
    self.is_enabled = False
    self.is_active = False
    self.gas_gating_active = False
    self.corner_radius_m = 0.0
    self.raw_v_target = CAP_INACTIVE
    self._cap = CurveSpeedCap(_DT)

  def _read_enabled_param(self) -> bool:
    if self.params is None:
      return True
    try:
      return bool(self.params.get_bool("SmartCruiseControlMap"))
    except Exception:
      return True

  def _raw_cap_from_map(self, v_cruise: float, trim: float) -> tuple[float, float]:
    """Returns (raw cap, curve speed of the governing point)."""
    pos = self._read_position()
    points = self._read_velocities()
    self.gov_lat = self.gov_lon = 0.0
    if pos is None or not points:
      return CAP_INACTIVE, 0.0

    try:
      lats = np.array([p["latitude"] for p in points], dtype=float)
      lons = np.array([p["longitude"] for p in points], dtype=float)
      tvs = np.array([p["velocity"] for p in points], dtype=float)
    except (KeyError, TypeError, ValueError):
      return CAP_INACTIVE, 0.0

    dists = _haversine_m(pos[0], pos[1], lats, lons)

    # locate ourselves on the route; only points ahead (route order) matter
    min_idx = int(np.argmin(dists))
    d_fwd = dists[min_idx:]
    tv_fwd = tvs[min_idx:]

    v_curve = np.maximum(tv_fwd * trim, _MIN_V_TARGET)
    # a point is a curve constraint only if mapd's UNtrimmed suggestion is
    # below cruise — otherwise trim < 1 would shave straight-road points too
    consider = (tv_fwd > 0) & (tv_fwd < v_cruise - 0.5) & (d_fwd <= _MAX_LOOKAHEAD_M) & np.isfinite(v_curve)
    if not np.any(consider):
      return CAP_INACTIVE, 0.0

    d_eff = np.maximum(0.0, d_fwd[consider] - v_curve[consider] * _ARRIVAL_LEAD_T)
    v_allowed = np.sqrt(v_curve[consider] ** 2 + 2.0 * _A_DECEL_APPROACH * d_eff)

    best = int(np.argmin(v_allowed))
    # v3.5.0: remember WHICH point won, so the onroad minimap can mark the exact
    # constraint the controller chose instead of re-deriving it from the same
    # array and drifting away from this code. Publishing beats duplicating.
    try:
      self.gov_lat = float(lats[min_idx:][consider][best])
      self.gov_lon = float(lons[min_idx:][consider][best])
    except Exception:
      self.gov_lat = self.gov_lon = 0.0
    return float(v_allowed[best]), float(v_curve[consider][best])

  def _advisory_cap(self, v_cruise: float) -> float:
    """v3.5.0. Cap implied by posted advisory speeds, or CAP_INACTIVE.

    Bounded twice on purpose: never below `advisory * ADVISORY_MARGIN` (the
    sign is the floor, we do not out-drive the engineer in either direction),
    and never more than ADVISORY_MAX_CUT below cruise, so a mistagged advisory
    on a motorway cannot produce an arbitrary slowdown. Returns CAP_INACTIVE
    on any doubt, which makes the caller's min() a no-op.
    """
    try:
      adv_now, adv_next, adv_d = self._read_advisory()
    except Exception:
      return CAP_INACTIVE

    cap = CAP_INACTIVE
    if adv_now is not None and adv_now > _ADVISORY_MIN:
      cap = adv_now * ADVISORY_MARGIN

    if adv_next is not None and adv_next > _ADVISORY_MIN and adv_d and adv_d > 0:
      v_a = adv_next * ADVISORY_MARGIN
      # same constant-decel approach envelope the curve points use, so an
      # advisory ahead tightens gradually instead of stepping at the sign
      d_eff = max(0.0, adv_d - v_a * _ARRIVAL_LEAD_T)
      cap = min(cap, math.sqrt(v_a * v_a + 2.0 * _A_DECEL_APPROACH * d_eff))

    if cap >= CAP_INACTIVE:
      return CAP_INACTIVE

    # the two bounds. max() here can only RAISE the cap, i.e. only ever soften
    # what the advisory is allowed to ask for.
    return max(cap, v_cruise - ADVISORY_MAX_CUT, _MIN_V_TARGET)

  def update(self, sm, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, v_cruise: float, fric: float) -> None:
    self.frame += 1
    if self.frame % _PARAM_CHECK_FRAMES == 0:
      self.enabled = self._read_enabled_param()

    self.is_enabled = long_enabled and self.enabled
    if not self.is_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    trim = speed_trim(fric)
    self.raw_v_target, v_curve = self._raw_cap_from_map(v_cruise, trim)

    # v3.5.0: a posted advisory limit may only ever LOWER the cap. It is folded
    # in before the debounce/smoothing so it inherits the same no-step seeding
    # and rate-limited release as everything else.
    self.advisory_v_target = self._advisory_cap(v_cruise)
    self.advisory_active = self.advisory_v_target < v_cruise - 1.0
    self.raw_v_target = min(self.raw_v_target, self.advisory_v_target)

    cap = self._cap.update(self.raw_v_target, v_ego, v_cruise)
    self.is_active = self._cap.active

    if self.is_active:
      self.state = "RELEASING" if self._cap.releasing else "ACTIVE"
      self.output_v_target = max(cap, _MIN_V_TARGET)
      self.output_a_target = a_ego  # display only; the MPC owns decel
      self.gas_gating_active = v_ego > self.output_v_target + 0.5
      # display-only estimate of the governing curve's radius from its speed
      tuning = get_tuning()
      self.corner_radius_m = (v_curve ** 2) / max(tuning.a_lat_target, 0.1) if v_curve > 0 else 0.0
    else:
      self.state = "INACTIVE"
      self.output_v_target = CAP_INACTIVE
      self.output_a_target = 0.0
      self.gas_gating_active = False
      self.corner_radius_m = 0.0

  def _reset(self):
    self.state = "INACTIVE"
    self.output_v_target = CAP_INACTIVE
    self.output_a_target = 0.0
    self.is_active = False
    self.gas_gating_active = False
    self.corner_radius_m = 0.0
    self.raw_v_target = CAP_INACTIVE
    self.advisory_active = False
    self.advisory_v_target = CAP_INACTIVE
    self.gov_lat = 0.0
    self.gov_lon = 0.0
    self._cap.reset()
