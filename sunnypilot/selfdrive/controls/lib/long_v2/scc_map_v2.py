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
# FunnyPilot v3.3.7: earlier, calmer approach. The old 1.0/2.0 envelope put
# most of the slowdown inside the turn entry ("slowing 20% mid-turn"); a
# gentler assumed decel plus a longer arrival lead moves the whole ramp
# BEFORE the corner, so by turn-in the speed is already the curve speed and
# the exit can be an acceleration.
_A_DECEL_APPROACH = 0.85  # m/s^2 comfortable approach decel budget
_ARRIVAL_LEAD_T = 3.5     # s — reach curve speed this early
_MAX_LOOKAHEAD_M = 400.0  # beyond this a curve cannot meaningfully constrain us
_FRIC_NOMINAL = 0.8
_FRIC_SCALE_MIN = 0.7
_FRIC_SCALE_MAX = 1.1
_PARAM_CHECK_FRAMES = 100

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


def speed_trim(fric: float) -> float:
  """Bounded trim on mapd's curve speeds: comfort preference x a bounded
  slice of the friction estimate (a steering-model value, not road grip)."""
  tuning = get_tuning()
  scale = min(_FRIC_SCALE_MAX, max(_FRIC_SCALE_MIN, fric / _FRIC_NOMINAL))
  return float(np.clip(tuning.sccm_speed_trim * scale, 0.5, 1.1))


class SCCMapV2:
  def __init__(self, params=None, position_reader=None, velocities_reader=None):
    self.params = params if params is not None else (Params() if Params is not None else None)
    self._read_position = position_reader or _default_position_reader
    self._read_velocities = velocities_reader or _default_velocities_reader
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
    self.governing_distance_m = 0.0  # v3.3.7: distance to the governing curve point (for the vision veto)
    self._cap = CurveSpeedCap(_DT)

  def _read_enabled_param(self) -> bool:
    if self.params is None:
      return True
    try:
      return bool(self.params.get_bool("SmartCruiseControlMap"))
    except Exception:
      return True

  def _raw_cap_from_map(self, v_cruise: float, trim: float) -> tuple[float, float, float]:
    """Returns (raw cap, curve speed of the governing point, distance to it)."""
    pos = self._read_position()
    points = self._read_velocities()
    if pos is None or not points:
      return CAP_INACTIVE, 0.0, 0.0

    try:
      lats = np.array([p["latitude"] for p in points], dtype=float)
      lons = np.array([p["longitude"] for p in points], dtype=float)
      tvs = np.array([p["velocity"] for p in points], dtype=float)
    except (KeyError, TypeError, ValueError):
      return CAP_INACTIVE, 0.0, 0.0

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
      return CAP_INACTIVE, 0.0, 0.0

    d_eff = np.maximum(0.0, d_fwd[consider] - v_curve[consider] * _ARRIVAL_LEAD_T)
    v_allowed = np.sqrt(v_curve[consider] ** 2 + 2.0 * _A_DECEL_APPROACH * d_eff)

    best = int(np.argmin(v_allowed))
    return float(v_allowed[best]), float(v_curve[consider][best]), float(d_fwd[consider][best])

  def update(self, sm, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, v_cruise: float, fric: float) -> None:
    self.frame += 1
    if self.frame % _PARAM_CHECK_FRAMES == 0:
      self.enabled = self._read_enabled_param()

    self.is_enabled = long_enabled and self.enabled
    if not self.is_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    trim = speed_trim(fric)
    self.raw_v_target, v_curve, self.governing_distance_m = self._raw_cap_from_map(v_cruise, trim)

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
    self.governing_distance_m = 0.0
    self._cap.reset()
