"""
FunnyPilot LongV2 — speed governor: applies all v_targets and selects minimum.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import weather_cap_active, weather_speed_scale
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import fuse_map_target

_V_CRUISE_MAX_MPS = 58.1  # ~130 mph


def gate_map_target(map_v_target: float, vision_is_active: bool, v_cruise: float = 0.0,
                    vision_corroboration: float = 0.0, advisory_active: bool = False) -> float:
  """SCC-M's cap as the governor should see it.

  v3.3.8 made this a binary veto: the map bound only while SCC-V was ACTIVE.
  v3.4.9 merges the two into one feature — corroboration is continuous and
  scales the map's authority instead of switching it, so real corners the
  model sees but has not (yet) crossed its own comfort threshold for stop
  being missed, while a map point on a straight road is still vetoed outright.
  See scc_fusion.py for the full rationale; this is a thin alias kept so the
  governor's import site and the older call shape both still work.
  """
  return fuse_map_target(map_v_target, v_cruise, vision_is_active, vision_corroboration, advisory_active)


class SpeedGovernor:
  def __init__(self):
    self.source = "cruise"
    self.weather_cap_active = False
    self.v_weather_cap = 999.0
    self.output_v_target = 999.0

  def update(
    self,
    v_cruise_raw: float,
    v_scc_map: float,
    v_scc_vision: float,
    v_sla: float,
    road_type: str,
    speed_limit_posted: float,
    fric: float,
    v_scc_learn: float = 999.0,
  ) -> float:
    tuning = get_tuning()

    # Road type hard cap (only when no posted limit)
    v_road_cap = _V_CRUISE_MAX_MPS
    if road_type and not speed_limit_posted:
      cap = tuning.road_type_caps.get(road_type)
      if cap:
        v_road_cap = cap

    # Weather cap
    self.weather_cap_active = weather_cap_active(fric)
    if self.weather_cap_active:
      scale = weather_speed_scale(fric)
      self.v_weather_cap = v_cruise_raw * scale
    else:
      self.v_weather_cap = _V_CRUISE_MAX_MPS

    candidates = {
      "cruise": v_cruise_raw,
      "scc_map": v_scc_map,
      "scc_vision": v_scc_vision,
      # v3.5.0 — the corner map this car built by driving (long_v2/scc_learn.py).
      # A separate candidate rather than folded into scc_map because its
      # authority comes from visit count, not from OSM, and the two must be
      # able to disagree without one silently masking the other.
      "scc_learn": v_scc_learn,
      "sla": v_sla,
      "road_cap": v_road_cap,
      "weather": self.v_weather_cap,
    }

    # Pick the most restrictive
    self.source = min(candidates, key=lambda k: candidates[k])
    self.output_v_target = candidates[self.source]
    return self.output_v_target
