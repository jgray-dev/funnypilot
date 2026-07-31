"""
FunnyPilot LongV2 tuning parameters — all constants in one place.
Override by writing JSON to Params key "LongV2Tuning".

v3.4.9: this dataclass is now EXACTLY the fields control code reads. It had
accumulated seven more — k_sccv/k_sccm (the pre-3.2.6e sqrt(fric*g) corner
formulas), thw_default/d_standstill/jerk_limit_*/decel_comfort/accel_comfort
(FollowingControllerV2, deleted in v3.2.6e when the MPC took back the
following problem) and speed_limit_offsets (never read; the resolver has its
own _offset_for_limit). They were kept "so existing param JSON blobs still
parse", but get_tuning() already drops unknown keys, so a stored blob
containing them parses fine either way — they bought nothing and read as
live tuning knobs, which is the expensive kind of dead code.
"""
import json
from dataclasses import dataclass, field, asdict

try:
  from openpilot.common.params import Params
except ImportError:
  Params = None


@dataclass
class LongV2Tuning:
  # v3.2.6e: comfort lateral acceleration target for curve speed control [m/s^2].
  # SCC-V slows so predicted lat accel stays under this; SCC-M uses it only to
  # estimate the displayed corner radius. Friction trims it by at most +/-30%.
  # v3.4.9: 2.4 -> 2.1. 2.4 m/s^2 is brisk for a corner taken by a machine
  # rather than a driver who chose the line, and it is one of the two reasons
  # corners were being missed (the other, the selection mask, is fixed in
  # scc_vision_v2.py). At 30 m/s, 2.1 m/s^2 first constrains a ~430 m radius —
  # a genuine sweeper, not lane-keeping wander.
  a_lat_target: float = 2.1
  # v3.2.6e: direct multiplier on mapd's suggested curve speeds (<1 = slower).
  sccm_speed_trim: float = 0.95
  # Road type hard caps [m/s]
  road_type_caps: dict[str, float] = field(default_factory=lambda: {
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
