"""
FunnyPilot LongV2 — SCC-Map v2 (physics cross-validated map speed targets).
"""
import json
import math

try:
  from openpilot.common.params import Params
  _mem_params = Params("/dev/shm/params")
except Exception:
  _mem_params = None

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning

_G = 9.81
_V_MIN_ACTIVE = 5.0
_PHYSICS_DIVERGE_THRESHOLD = 0.15   # if mapd target differs >15% from physics, prefer physics
_BRAKING_BUFFER_M = 15.0
_MIN_V_TARGET = 3.0


def _corner_speed(fric: float, radius_m: float, k: float) -> float:
  if radius_m <= 0:
    return 999.0
  return k * math.sqrt(fric * _G * radius_m)


class SCCMapV2:
  def __init__(self):
    self.state = "INACTIVE"
    self.output_v_target = 999.0
    self.output_a_target = 0.0
    self.is_enabled = False
    self.is_active = False
    self.gas_gating_active = False
    self.corner_radius_m = 0.0
    self._v_target_smooth = 999.0
    self._active_frames = 0

  def _read_map_targets(self):
    if _mem_params is None:
      return []
    try:
      raw = _mem_params.get("MapTargetVelocities")
      if not raw:
        return []
      return json.loads(raw)
    except Exception:
      return []

  def _corner_unwind(self, sm) -> bool:
    try:
      curvature_rates = sm["lateralPlan"].curvatureRates
      if curvature_rates:
        return curvature_rates[0] < 0
    except Exception:
      pass
    return False

  def update(self, sm, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, v_cruise: float, fric: float) -> None:
    tuning = get_tuning()
    self.is_enabled = long_enabled

    if not long_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    targets = self._read_map_targets()
    if not targets:
      self._reset()
      return

    best_v = 999.0
    best_radius = 0.0

    for entry in targets:
      try:
        v_mapd = float(entry.get("v", 999.0))
        dist = float(entry.get("dist", 999.0))
        radius = float(entry.get("radius", 0.0))
      except (TypeError, ValueError):
        continue

      if v_mapd <= 0:
        continue

      if radius > 0:
        # Cross-validate with physics
        v_physics = _corner_speed(fric, radius, tuning.k_sccm)
        diff = abs(v_mapd - v_physics) / max(v_physics, 1.0)
        if diff > _PHYSICS_DIVERGE_THRESHOLD:
          v_target = v_physics  # trust physics more
        else:
          v_target = min(v_mapd, v_physics)
        v_target = max(_MIN_V_TARGET, v_target)
        if v_target < best_v:
          best_v = v_target
          best_radius = radius
      else:
        # No radius data — just use mapd target directly
        if v_mapd < best_v:
          best_v = v_mapd
          best_radius = 0.0

    self.corner_radius_m = best_radius

    # Check if we should be braking: braking-start distance check
    should_activate = best_v < v_ego - 0.5

    # Corner unwind detection — if curvature is decreasing, start releasing
    unwinding = self._corner_unwind(sm) and self.is_active

    # Always smooth and publish best_v (visible in debug UI even when inactive)
    if best_v < self._v_target_smooth:
      alpha = 0.1
    else:
      alpha = 0.3
    self._v_target_smooth = self._v_target_smooth * (1 - alpha) + best_v * alpha
    self.output_v_target = self._v_target_smooth

    if should_activate and not unwinding:
      self.is_active = True
      self._active_frames += 1
      self.output_a_target = a_ego
      self.gas_gating_active = v_ego > self.output_v_target + 1.0
      self.state = "ACTIVE"
    else:
      if unwinding:
        self.state = "UNWINDING"
        self.is_active = True
        self.output_a_target = 0.0
        self.gas_gating_active = False
      else:
        self.state = "INACTIVE"
        self.is_active = False
        self.output_a_target = 0.0
        self.gas_gating_active = False

  def _reset(self):
    self.state = "INACTIVE"
    self.output_v_target = 999.0
    self.output_a_target = 0.0
    self.is_active = False
    self.gas_gating_active = False
    self._v_target_smooth = 999.0
    self._active_frames = 0
