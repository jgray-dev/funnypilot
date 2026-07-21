"""
FunnyPilot LongV2 — speed governor: applies all v_targets and selects minimum.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import weather_cap_active, weather_speed_scale
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE

_V_CRUISE_MAX_MPS = 58.1  # ~130 mph


def gate_map_target(map_v_target: float, vision_is_active: bool) -> float:
  """v3.3.8: SCC-M may only narrow SCC-V's cap, never introduce one on its
  own — map route data is far more prone to false positives (mistagged/
  rounded curve speeds, stale OSM data) than the model's own view of the
  road. Returns CAP_INACTIVE when vision does not also think a reduction is
  warranted, regardless of what the map suggests."""
  return map_v_target if vision_is_active else CAP_INACTIVE


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
