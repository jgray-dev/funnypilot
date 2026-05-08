"""
FunnyPilot LongV2 tuning parameters — all constants in one place.
Override by writing JSON to Params key "LongV2Tuning".
"""
import json
from dataclasses import dataclass, field, asdict
from typing import Dict

try:
  from openpilot.common.params import Params
except ImportError:
  Params = None


@dataclass
class LongV2Tuning:
  # SCC-V threshold scaling factor (fraction of μg that triggers corner slow)
  k_sccv: float = 0.72
  # SCC-M physics cross-validation scale factor
  k_sccm: float = 0.78
  # Default time headway [s]
  thw_default: float = 1.8
  # Standstill gap [m]
  d_standstill: float = 5.0
  # Normal jerk limit [m/s³]
  jerk_limit_normal: float = 0.5
  # Safety jerk limit [m/s³]
  jerk_limit_safety: float = 3.0
  # Comfort deceleration [m/s²]
  decel_comfort: float = 1.8
  # Comfort acceleration [m/s²]
  accel_comfort: float = 1.5
  # Speed limit offsets by road type [m/s]
  speed_limit_offsets: Dict[str, float] = field(default_factory=lambda: {
    "motorway": 3.13,       # +7 mph
    "trunk": 2.24,          # +5 mph
    "primary": 2.24,        # +5 mph
    "secondary": 2.24,      # +5 mph
    "tertiary": 0.89,       # +2 mph
    "residential": 0.89,    # +2 mph
    "living_street": 0.0,
    "service": 0.0,
  })
  # Road type hard caps [m/s]
  road_type_caps: Dict[str, float] = field(default_factory=lambda: {
    "living_street": 8.94,  # 20 km/h
    "service": 8.94,        # 20 km/h
    "residential": 11.11,   # 40 km/h (only when no posted limit)
  })


_tuning_cache: LongV2Tuning | None = None


def get_tuning() -> LongV2Tuning:
  global _tuning_cache
  if _tuning_cache is not None:
    return _tuning_cache
  if Params is not None:
    try:
      raw = Params().get("LongV2Tuning")
      if raw:
        d = json.loads(raw)
        base = asdict(LongV2Tuning())
        base.update({k: v for k, v in d.items() if k in base})
        _tuning_cache = LongV2Tuning(**base)
        return _tuning_cache
    except Exception:
      pass
  _tuning_cache = LongV2Tuning()
  return _tuning_cache


def reset_tuning_cache() -> None:
  global _tuning_cache
  _tuning_cache = None
