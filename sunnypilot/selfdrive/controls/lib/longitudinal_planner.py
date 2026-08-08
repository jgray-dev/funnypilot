"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import time

from cereal import messaging, custom
from opendbc.car import structs
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX
from openpilot.sunnypilot.selfdrive.controls.lib.dec.dec import DynamicExperimentalController
from openpilot.sunnypilot.selfdrive.controls.lib.e2e_alerts_helper import E2EAlertsHelper
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import SpeedLimitAssist
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_resolver import SpeedLimitResolver
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.sla_shm import write_sla_shm
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP
from openpilot.sunnypilot.models.helpers import get_active_bundle

# LongV2 components (speed-domain governors only; following is owned by the
# MPC as of v3.2.6e — see selfdrive/controls/lib/longitudinal_planner.py)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import get_fric
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_vision_v2 import SCCVisionV2
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_map_v2 import SCCMapV2, read_gps
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.speed_governor import SpeedGovernor, gate_map_target
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.corner_effort import (
  lane_departure_m as _lane_departure_m)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import (
  write_scc_shm, write_learn_shm, write_corners_shm, write_scc_debug_shm,
  read_eps_limited as _read_eps_limited, read_pitch_rate as _read_pitch_rate)

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
    self._scc_map_authority = 0.0
    # v3.6.2 — SCC-M v2 watches the car at the carState rate, not the model
    # rate, because the oscillation it is looking for lives at a few Hz and
    # would alias at 20. `update_car_state` below is that hook.
    self._last_cs_t = 0.0

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

  def update_car_state_sp(self, sm) -> None:
    """v3.6.2 — SCC-M v2's learning hook, called at the carState rate.

    THE RATE IS THE POINT. The signals that decide whether a corner was taken
    too fast — steering reversals, the torque clamp biting, the controller
    saturating — are a few Hz, and sampled at the planner's 20 Hz they would
    alias into something meaningless. plannerd's loop already polls carState at
    100 Hz for `sla.update_car_state`, so this costs a second call on a loop
    that was running anyway.

    THE MEASURED CURVATURE COMES FROM controlsState, NOT FROM modelV2.
    `controlsState.curvature` is the vehicle model's reading of the STEERING
    ANGLE — what the car is actually doing, engaged or not.
    `modelV2.orientationRate` is the model's PLAN, and the model plans to slow
    for corners, so it reports intent rather than fact (the v3.4.9 / v3.5.4
    trap). `carState.yawRate` would be the obvious third option and is a silent
    zero here: only PSA and Ford populate it in opendbc.

    v3.6.2 also feeds the PITCH RATE from the same lat_interp heartbeat the
    EPS-limited flag comes from. It is what lets corner_effort tell "this
    corner is too fast" from "the road just hit us" — signals that are
    identical in the steering trace, and which this car has a documented
    history of confusing (the v3.3.8 railroad-crossing investigation).

    Total — any failure degrades to "this frame was not observed".
    """
    try:
      CS = sm['carState']
      cst = sm['controlsState']
      now = time.monotonic()
      if self._last_cs_t and now - self._last_cs_t < 1e-4:
        return
      self._last_cs_t = now

      saturated = False
      try:
        lcs = cst.lateralControlState
        saturated = bool(getattr(lcs, lcs.which()).saturated)
      except Exception:
        pass

      # v3.6.4 — HOW FAR OUT OF THE LANE DID WE GET. The capnp unpacking lives
      # here; the geometry is a pure function in corner_effort so it can be
      # tested without a model. Any doubt yields 0.0 = "inside the lane", which
      # is the same answer as "cannot tell" on purpose: an unreadable lane must
      # contribute NO stress, so a model that has lost the lines can only ever
      # make a pass look cleaner than it was.
      departure, lane_change = 0.0, False
      try:
        md = sm['modelV2']
        lanes = md.laneLines
        probs = md.laneLineProbs
        if len(lanes) >= 3 and len(probs) >= 3 and len(lanes[1].y) and len(lanes[2].y):
          departure = _lane_departure_m(float(lanes[1].y[0]), float(lanes[2].y[0]),
                                        float(probs[1]), float(probs[2]))
        lane_change = md.meta.laneChangeState != 0    # 0 == LaneChangeState.off
      except Exception:
        departure, lane_change = 0.0, False

      _lat, _lon, _brg, acc, _ok = read_gps(sm)
      self._scc_map_v2.observe_frame(
        now, float(CS.vEgo), float(cst.curvature), float(CS.steeringAngleDeg),
        float(CS.steeringTorque), bool(sm['carControl'].latActive), saturated,
        _read_eps_limited(), bool(CS.leftBlinker or CS.rightBlinker),
        bool(CS.standstill), acc, _read_pitch_rate(), departure, lane_change)
    except Exception:
      pass

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

    # LongV2: SCC-Map v2 (v3.6.2) — corner radius measured from the route
    # polyline, lateral budget learned from how this car actually drives each
    # bend. Nothing here reads a speed off the map any more.
    lat, lon, bearing, _acc, gps_ok = read_gps(sm)
    self._scc_map_v2.update(long_enabled, v_ego, a_ego, v_cruise, lat, lon, bearing, gps_ok)
    self._scc_map_v2.flush(time.monotonic())

    # The corroboration gate. A corner we have driven before bypasses it; an
    # unvisited one still has to be agreed with by the model or vouched for by
    # proximity, because the SHAPE is still OSM's even though the SPEED is ours.
    # See long_v2/scc_fusion.py.
    v_scc_vision = self._scc_vision_v2.output_v_target
    v_scc_map = gate_map_target(self._scc_map_v2.output_v_target, self._scc_vision_v2.is_active,
                                v_cruise, self._scc_vision_v2.corroboration,
                                self._scc_map_v2.gov_confidence,
                                self._scc_map_v2.gov_distance,
                                v_ego)
    # v3.5.0: what fraction of the cut SCC-M asked for actually survived the
    # fusion. 1.0 = passed through whole, 0.0 = vetoed. The onroad minimap draws
    # this as a solid vs hollow marker; publishing it beats having the UI
    # re-derive a selection rule that lives here.
    asked = max(0.0, v_cruise - self._scc_map_v2.output_v_target)
    got = max(0.0, v_cruise - v_scc_map) if v_scc_map < 999.0 else 0.0
    self._scc_map_authority = min(1.0, got / asked) if asked > 0.1 else 0.0
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

    # FunnyPilot v3.4.1: the SLA set-speed ramp target and gas-gate flag are
    # published via /dev/shm instead of new capnp fields. A .capnp change forces
    # a SCons rebuild of the compiled schema on the device, which this fork
    # avoids on principle; these paths are Python + raylib end to end, so a
    # plain file needs no compilation (same pattern as /dev/shm/lat_interp).
    # NOTE (v3.4.3): the capnp change did NOT cause the v3.4.0 boot failure —
    # that was a `car.CarState | None` annotation in cruise_ext.py. See sla_shm.py.
    write_sla_shm(self.sla.v_cruise_target, self.sla.gas_gate_active)

    # v3.5.0: SCC-M's governing corner + how much authority it kept, for the
    # onroad minimap. Diagnostic only — see long_v2/scc_shm.py.
    write_scc_shm(self._scc_map_v2.gov_lat, self._scc_map_v2.gov_lon,
                  self._scc_map_v2.output_v_target, self._scc_map_authority,
                  self._scc_map_v2.gov_confidence > 0.0)

    # v3.6.2: every corner ahead with the speed we chose for it, so the minimap
    # can tint the road by the slowdown each one needs. The UI cannot compute
    # these — the learned half lives in a store on /data and nothing in the HUD
    # may touch a filesystem.
    write_corners_shm(self._scc_map_v2.corners)

    # v3.6.2: the dev-UI payload. Diagnostic only — see long_v2/scc_shm.py.
    write_scc_debug_shm(self._scc_map_v2.debug_row(self._scc_map_authority))

    # v3.5.0: the learned corner count + whether it is governing right now.
    write_learn_shm(self._scc_map_v2.learned_count, self._scc_map_v2.is_active,
                    self._scc_map_v2.gov_confidence)

    # E2E Alerts
    e2eAlerts = longitudinalPlanSP.e2eAlerts
    e2eAlerts.greenLightAlert = self.e2e_alerts_helper.green_light_alert
    e2eAlerts.leadDepartAlert = self.e2e_alerts_helper.lead_depart_alert

    # LongV2: friction + weather cap
    longitudinalPlanSP.frictionCoefficient = float(self._fric)
    longitudinalPlanSP.weatherCapActive = bool(self._speed_governor.weather_cap_active)
    longitudinalPlanSP.vWeatherCap = float(self._speed_governor.v_weather_cap)

    pm.send('longitudinalPlanSP', plan_sp_send)
