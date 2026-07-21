"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from cereal import messaging, custom
from opendbc.car import structs
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX
from openpilot.sunnypilot.selfdrive.controls.lib.dec.dec import DynamicExperimentalController
from openpilot.sunnypilot.selfdrive.controls.lib.e2e_alerts_helper import E2EAlertsHelper
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import SpeedLimitAssist
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_resolver import SpeedLimitResolver
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP
from openpilot.sunnypilot.models.helpers import get_active_bundle

# LongV2 components (speed-domain governors only; following is owned by the
# MPC as of v3.2.6e — see selfdrive/controls/lib/longitudinal_planner.py)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import get_fric
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_vision_v2 import SCCVisionV2
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_map_v2 import SCCMapV2
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.speed_governor import SpeedGovernor, gate_map_target

DecState = custom.LongitudinalPlanSP.DynamicExperimentalControl.DynamicExperimentalControlState
LongitudinalPlanSource = custom.LongitudinalPlanSP.LongitudinalPlanSource


class LongitudinalPlannerSP:
  def __init__(self, CP: structs.CarParams, CP_SP: structs.CarParamsSP, mpc):
    self.events_sp = EventsSP()
    self.resolver = SpeedLimitResolver()
    self.dec = DynamicExperimentalController(CP, mpc)
    self.resolver = SpeedLimitResolver()
    self.sla = SpeedLimitAssist(CP, CP_SP)
    self.generation = int(model_bundle.generation) if (model_bundle := get_active_bundle()) else None
    self.source = LongitudinalPlanSource.cruise
    self.e2e_alerts_helper = E2EAlertsHelper()

    self.output_v_target = 0.
    self.output_a_target = 0.

    # LongV2 components
    self._scc_vision_v2 = SCCVisionV2()
    self._scc_map_v2 = SCCMapV2()
    self._speed_governor = SpeedGovernor()
    self._fric = 0.8

  @property
  def mlsim(self) -> bool:
    # If we don't have a generation set, we assume it's default model. Which as of today are mlsim.
    return bool(self.generation is None or self.generation >= 11)

  def get_mpc_mode(self) -> str | None:
    # v3.3.8: DEC owns the acc/blended decision while it is active
    # (experimental mode on + DynamicExperimentalControl toggle on)
    if not self.dec.active():
      return None

    return self.dec.mode()

  def update_targets(self, sm: messaging.SubMaster, v_ego: float, a_ego: float, v_cruise: float) -> tuple[float, float]:
    CS = sm['carState']
    v_cruise_cluster_kph = min(CS.vCruiseCluster, V_CRUISE_MAX)
    v_cruise_cluster = v_cruise_cluster_kph * CV.KPH_TO_MS

    long_enabled = sm['carControl'].enabled
    long_override = sm['carControl'].cruiseControl.override

    # Update friction estimate
    self._fric = get_fric(sm)

    # Speed Limit Resolver
    self.resolver.update(v_ego, sm)

    # Speed Limit Assist (v3.3.3: resolver ahead info feeds the pre-zone gas gate)
    has_speed_limit = self.resolver.speed_limit_valid or self.resolver.speed_limit_last_valid
    self.sla.update(long_enabled, long_override, v_ego, a_ego, v_cruise_cluster, self.resolver.speed_limit,
                    self.resolver.speed_limit_final_last, has_speed_limit, self.resolver.distance, self.events_sp,
                    next_speed_limit_final=self.resolver.next_speed_limit_final,
                    next_distance=self.resolver.distance_to_next_limit)

    # LongV2: SCC-Vision v2
    self._scc_vision_v2.update(sm, long_enabled, long_override, v_ego, a_ego, v_cruise, self._fric)

    # LongV2: SCC-Map v2
    self._scc_map_v2.update(sm, long_enabled, long_override, v_ego, a_ego, v_cruise, self._fric)

    # LongV2: Speed governor selects minimum of all v_targets. SCC-M requires
    # SCC-V agreement to bind (gate_map_target) — vision alone retains full
    # authority to slow the car; map alone cannot.
    v_scc_vision = self._scc_vision_v2.output_v_target
    v_scc_map = gate_map_target(self._scc_map_v2.output_v_target, self._scc_vision_v2.is_active)
    v_sla = self.sla.output_v_target if self.sla.is_active else 999.0

    # Speed limit info for road cap logic
    road_type = ""
    speed_limit_posted = self.resolver.speed_limit if self.resolver.speed_limit_valid else 0.0
    try:
      road_type = sm["roadLimitSpeed"].roadType if sm.updated.get("roadLimitSpeed") else ""
    except Exception:
      pass

    v_governed = self._speed_governor.update(
      v_cruise, v_scc_map, v_scc_vision, v_sla,
      road_type, speed_limit_posted, self._fric
    )

    # Source tracking — prefer most restrictive non-cruise source for display
    if v_governed < v_cruise - 0.5:
      src = self._speed_governor.source
      if src == "scc_vision":
        self.source = LongitudinalPlanSource.sccVision
      elif src == "scc_map":
        self.source = LongitudinalPlanSource.sccMap
      elif src == "sla":
        self.source = LongitudinalPlanSource.speedLimitAssist
      else:
        self.source = LongitudinalPlanSource.cruise
    else:
      self.source = LongitudinalPlanSource.cruise

    self.output_v_target = v_governed
    self.output_a_target = a_ego
    return self.output_v_target, self.output_a_target

  def update(self, sm: messaging.SubMaster) -> None:
    self.events_sp.clear()
    self.dec.update(sm)
    self.e2e_alerts_helper.update(sm, self.events_sp)

  def publish_longitudinal_plan_sp(self, sm: messaging.SubMaster, pm: messaging.PubMaster) -> None:
    plan_sp_send = messaging.new_message('longitudinalPlanSP')

    plan_sp_send.valid = sm.all_checks(service_list=['carState', 'controlsState'])

    longitudinalPlanSP = plan_sp_send.longitudinalPlanSP
    longitudinalPlanSP.longitudinalPlanSource = self.source
    longitudinalPlanSP.vTarget = float(self.output_v_target)
    longitudinalPlanSP.aTarget = float(self.output_a_target)
    longitudinalPlanSP.events = self.events_sp.to_msg()

    # Dynamic Experimental Control
    dec = longitudinalPlanSP.dec
    dec.state = DecState.blended if self.dec.mode() == 'blended' else DecState.acc
    dec.enabled = self.dec.enabled()
    dec.active = self.dec.active()

    # Smart Cruise Control — use LongV2 SCC-V and SCC-M
    smartCruiseControl = longitudinalPlanSP.smartCruiseControl
    # Vision Control (LongV2)
    sccVision = smartCruiseControl.vision
    sccVision.state = 0  # disabled placeholder; LongV2 uses its own state machine
    sccVision.vTarget = float(self._scc_vision_v2.output_v_target)
    sccVision.aTarget = float(self._scc_vision_v2.output_a_target)
    sccVision.currentLateralAccel = 0.0
    sccVision.maxPredictedLateralAccel = 0.0
    sccVision.enabled = self._scc_vision_v2.is_enabled
    sccVision.active = self._scc_vision_v2.is_active
    sccVision.gasGating = bool(self._scc_vision_v2.gas_gating_active)
    # Map Control (LongV2)
    sccMap = smartCruiseControl.map
    sccMap.state = 0  # disabled placeholder
    sccMap.vTarget = float(self._scc_map_v2.output_v_target)
    sccMap.aTarget = float(self._scc_map_v2.output_a_target)
    sccMap.enabled = self._scc_map_v2.is_enabled
    sccMap.active = self._scc_map_v2.is_active
    sccMap.gasGating = bool(self._scc_map_v2.gas_gating_active)
    sccMap.cornerRadiusAhead = float(self._scc_map_v2.corner_radius_m)

    # Speed Limit
    speedLimit = longitudinalPlanSP.speedLimit
    resolver = speedLimit.resolver
    resolver.speedLimit = float(self.resolver.speed_limit)
    resolver.speedLimitLast = float(self.resolver.speed_limit_last)
    resolver.speedLimitFinal = float(self.resolver.speed_limit_final)
    resolver.speedLimitFinalLast = float(self.resolver.speed_limit_final_last)
    resolver.speedLimitValid = self.resolver.speed_limit_valid
    resolver.speedLimitLastValid = self.resolver.speed_limit_last_valid
    resolver.speedLimitOffset = float(self.resolver.speed_limit_offset)
    resolver.distToSpeedLimit = float(self.resolver.distance)
    resolver.source = self.resolver.source
    assist = speedLimit.assist
    assist.state = self.sla.state
    assist.enabled = self.sla.is_enabled
    assist.active = self.sla.is_active
    assist.vTarget = float(self.sla.output_v_target)
    assist.aTarget = float(self.sla.output_a_target)
    assist.slaLocked = bool(self.sla.sla_locked)
    assist.slaDynamicOffset = float(self.sla.dynamic_offset_ratio)

    # E2E Alerts
    e2eAlerts = longitudinalPlanSP.e2eAlerts
    e2eAlerts.greenLightAlert = self.e2e_alerts_helper.green_light_alert
    e2eAlerts.leadDepartAlert = self.e2e_alerts_helper.lead_depart_alert

    # LongV2: friction + weather cap
    longitudinalPlanSP.frictionCoefficient = float(self._fric)
    longitudinalPlanSP.weatherCapActive = bool(self._speed_governor.weather_cap_active)
    longitudinalPlanSP.vWeatherCap = float(self._speed_governor.v_weather_cap)

    pm.send('longitudinalPlanSP', plan_sp_send)
