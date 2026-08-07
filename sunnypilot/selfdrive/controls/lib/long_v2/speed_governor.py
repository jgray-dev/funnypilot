"""
FunnyPilot LongV2 — speed governor: applies all v_targets and selects minimum.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import weather_cap_active, weather_speed_scale
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import fuse_map_target

_V_CRUISE_MAX_MPS = 58.1  # ~130 mph


def gate_map_target(map_v_target: float, vision_is_active: bool, v_cruise: float = 0.0,
                    vision_corroboration: float = 0.0, learned_conf: float = 0.0,
                    dist_m: float = 0.0, v_ego: float = 0.0) -> float:
  """SCC-M v2's cap as the governor should see it.

  A thin alias so the governor's import site keeps one name for the operation.
  See scc_fusion.py for why the gate still exists now that the corner speed is
  ours, and why a corner we have driven bypasses it.
  """
  return fuse_map_target(map_v_target, v_cruise, vision_is_active, vision_corroboration,
                         learned_conf, dist_m, v_ego)


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
      # v3.6.2 — ONE SCC-M candidate. v3.5.0 fed a separate `scc_learn` entry
      # here because the learned speed and OSM's speed were different KINDS of
      # claim that had to be able to disagree. SCC-M v2 measures the geometry
      # and learns the budget for the same corner, so there is one claim, and a
      # second entry would only have let the min() hide which was speaking.
      "scc_map": v_scc_map,
      "scc_vision": v_scc_vision,
      "sla": v_sla,
      "road_cap": v_road_cap,
      "weather": self.v_weather_cap,
    }

    # Pick the most restrictive
    self.source = min(candidates, key=lambda k: candidates[k])
    self.output_v_target = candidates[self.source]
    return self.output_v_target
