"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import time

import cereal.messaging as messaging
from cereal import custom
from openpilot.common.constants import CV
from openpilot.common.gps import get_gps_location_service
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD, get_sanitize_int_param
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Policy, OffsetType

SpeedLimitSource = custom.LongitudinalPlanSP.SpeedLimit.Source

# FunnyPilot v3.4.5 — CLOCK DOMAIN FIX. The upstream code computed map-data age
# as `time.monotonic() - gpsLocation.unixTimestampMillis * 1e-3`. Those are two
# DIFFERENT clocks: monotonic counts seconds since boot (~1e4), the GPS stamp is
# a Unix epoch (~1.8e9, qcomgpsd.py sets it from `dt.timestamp()`). The
# difference is about -1.785e9 s, so:
#   * the staleness gate `age > 10` was never true for any real fix (it only
#     rejected `unixTimestampMillis == 0`, i.e. "no fix ever") — an accidental
#     has-ever-had-a-fix test, not a freshness test;
#   * `distance_since_fix = v_ego * age` came out around -3.9e10 m at highway
#     speed, so `distance_to_next_limit` was ~3.9e10 m instead of metres. Every
#     consumer of that distance (SLA's pre-zone gas gate, and the v3.4.0
#     set-speed ramp) was therefore DEAD CODE on a moving car — which is
#     precisely the "it only starts adjusting once we enter the new zone"
#     report this version fixes.
# The correct age is measured in the CONSUMER's own clock: SubMaster stamps
# `recv_time[s]` with `time.monotonic()` when it receives a message, regardless
# of what language published it. Do NOT substitute `logMonoTime` here —
# cereal/messaging stamps that with time.monotonic() for Python publishers but
# CLOCK_BOOTTIME for C++ ones, which would reintroduce this exact bug class.
MAP_MSG_MAX_AGE = 2.0  # s — liveMapDataSP is 1 Hz, so this tolerates one drop

ALL_SOURCES = tuple(SpeedLimitSource.schema.enumerants.values())


class SpeedLimitResolver:
  limit_solutions: dict[custom.LongitudinalPlanSP.SpeedLimit.Source, float]
  distance_solutions: dict[custom.LongitudinalPlanSP.SpeedLimit.Source, float]
  v_ego: float
  speed_limit: float
  speed_limit_last: float
  speed_limit_final: float
  speed_limit_final_last: float
  distance: float
  source: custom.LongitudinalPlanSP.SpeedLimit.Source
  speed_limit_offset: float

  def __init__(self):
    self.params = Params()
    self.frame = -1

    self._gps_location_service = get_gps_location_service(self.params)
    self.limit_solutions = {}  # Store for speed limit solutions from different sources
    self.distance_solutions = {}  # Store for distance to current speed limit start for different sources

    self.policy = self.params.get("SpeedLimitPolicy", return_default=True)
    self.policy = get_sanitize_int_param(
      "SpeedLimitPolicy",
      Policy.min().value,
      Policy.max().value,
      self.params
    )
    self._policy_to_sources_map = {
      Policy.car_state_only: [SpeedLimitSource.car],
      Policy.map_data_only: [SpeedLimitSource.map],
      Policy.car_state_priority: [SpeedLimitSource.car, SpeedLimitSource.map],
      Policy.map_data_priority: [SpeedLimitSource.map, SpeedLimitSource.car],
      Policy.combined: [SpeedLimitSource.car, SpeedLimitSource.map],
    }
    self.source = SpeedLimitSource.none
    for source in ALL_SOURCES:
      self._reset_limit_sources(source)

    self.is_metric = self.params.get_bool("IsMetric")
    self.offset_type = get_sanitize_int_param(
      "SpeedLimitOffsetType",
      OffsetType.min().value,
      OffsetType.max().value,
      self.params
    )
    self.offset_value = self.params.get("SpeedLimitValueOffset", return_default=True)

    self.speed_limit = 0.
    self.speed_limit_last = 0.
    self.speed_limit_final = 0.
    self.speed_limit_final_last = 0.
    self.speed_limit_offset = 0.

    # FunnyPilot v3.3.3: upcoming zone info (map source), exposed for SLA
    # pre-zone gas gating. next_speed_limit_final includes the static offset.
    self.next_speed_limit = 0.
    self.next_speed_limit_final = 0.
    self.distance_to_next_limit = 0.

  def update_speed_limit_states(self) -> None:
    self.speed_limit_final = self.speed_limit + self.speed_limit_offset

    if self.speed_limit > 0.:
      self.speed_limit_last = self.speed_limit
      self.speed_limit_final_last = self.speed_limit_final

  @property
  def speed_limit_valid(self) -> bool:
    return self.speed_limit > 0.

  @property
  def speed_limit_last_valid(self) -> bool:
    return self.speed_limit_last > 0.

  def update_params(self):
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.policy = self.params.get("SpeedLimitPolicy", return_default=True)
      self.is_metric = self.params.get_bool("IsMetric")
      self.offset_type = self.params.get("SpeedLimitOffsetType", return_default=True)
      self.offset_value = self.params.get("SpeedLimitValueOffset", return_default=True)

  def _get_speed_limit_offset(self) -> float:
    return self._offset_for_limit(self.speed_limit)

  def _offset_for_limit(self, limit: float) -> float:
    # FunnyPilot v3.3.3: offset computation parameterized on the limit so the
    # UPCOMING zone's final target can be computed the same way as the current one.
    if self.offset_type == OffsetType.off:
      return 0
    elif self.offset_type == OffsetType.fixed:
      return float(self.offset_value * (CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS))
    elif self.offset_type == OffsetType.percentage:
      return float(self.offset_value * 0.01 * limit)
    else:
      raise NotImplementedError("Offset not supported")

  def _reset_limit_sources(self, source: custom.LongitudinalPlanSP.SpeedLimit.Source) -> None:
    self.limit_solutions[source] = 0.
    self.distance_solutions[source] = 0.

  def _get_from_car_state(self, sm: messaging.SubMaster) -> None:
    self._reset_limit_sources(SpeedLimitSource.car)
    self.limit_solutions[SpeedLimitSource.car] = sm['carStateSP'].speedLimit
    self.distance_solutions[SpeedLimitSource.car] = 0.

  def _get_from_map_data(self, sm: messaging.SubMaster) -> None:
    self._reset_limit_sources(SpeedLimitSource.map)
    self.next_speed_limit = 0.
    self.next_speed_limit_final = 0.
    self.distance_to_next_limit = 0.
    self._process_map_data(sm)

  def _process_map_data(self, sm: messaging.SubMaster) -> None:
    map_data = sm['liveMapDataSP']

    # recv_time defaults to 0. until the first message arrives, so this also
    # rejects "no map data yet" without a separate check. sm.valid for this
    # service is literally llk.gpsOK (base_map_data.py), i.e. the GPS-fix test
    # the old epoch subtraction was reaching for.
    map_age = max(0., time.monotonic() - sm.recv_time['liveMapDataSP'])
    if not sm.valid['liveMapDataSP'] or map_age > MAP_MSG_MAX_AGE:
      return

    speed_limit = map_data.speedLimit if map_data.speedLimitValid else 0.
    next_speed_limit = map_data.speedLimitAhead if map_data.speedLimitAheadValid else 0.

    self._calculate_map_data_limits(sm, speed_limit, next_speed_limit, map_age)

  def _calculate_map_data_limits(self, sm: messaging.SubMaster, speed_limit: float, next_speed_limit: float,
                                 map_age: float = 0.) -> None:
    map_data = sm['liveMapDataSP']

    # map_age is passed in, not recomputed: the caller already gated on it and
    # the two must not be able to disagree.
    distance_since_fix = self.v_ego * map_age
    distance_to_speed_limit_ahead = max(0., map_data.speedLimitAheadDistance - distance_since_fix)

    self.limit_solutions[SpeedLimitSource.map] = speed_limit
    self.distance_solutions[SpeedLimitSource.map] = 0.

    # FunnyPilot v3.3.3: the upstream early-switch (adopt the upcoming limit
    # within an adapt distance, marked FIXME "not working as expected") is
    # REMOVED: switching speed_limit before the boundary made the cruise
    # set-speed snap fire early, i.e. BRAKING before the zone. The resolved
    # limit now changes exactly at the boundary; slowing down beforehand is
    # SLA's pre-zone gas gate (throttle-only, speed_limit_assist.py) using
    # the ahead info exposed below.
    if next_speed_limit > 0.:
      self.next_speed_limit = next_speed_limit
      self.next_speed_limit_final = next_speed_limit + self._offset_for_limit(next_speed_limit)
      self.distance_to_next_limit = distance_to_speed_limit_ahead

  def _get_source_solution_according_to_policy(self) -> custom.LongitudinalPlanSP.SpeedLimit.Source:
    sources_for_policy = self._policy_to_sources_map[self.policy]

    if self.policy != Policy.combined:
      # They are ordered in the order of preference, so we pick the first that's non-zero
      for source in sources_for_policy:
        if self.limit_solutions[source] > 0.:
          return source
      return SpeedLimitSource.none

    sources_with_limits = [(s, limit) for s, limit in [(s, self.limit_solutions[s]) for s in sources_for_policy] if limit > 0.]
    if sources_with_limits:
      return min(sources_with_limits, key=lambda x: x[1])[0]

    return SpeedLimitSource.none

  def _resolve_limit_sources(self, sm: messaging.SubMaster) -> tuple[float, float, custom.LongitudinalPlanSP.SpeedLimit.Source]:
    """Get limit solutions from each data source"""
    self._get_from_car_state(sm)
    self._get_from_map_data(sm)

    source = self._get_source_solution_according_to_policy()
    speed_limit = self.limit_solutions[source] if source else 0.
    distance = self.distance_solutions[source] if source else 0.

    return speed_limit, distance, source

  def update(self, v_ego: float, sm: messaging.SubMaster) -> None:
    self.v_ego = v_ego
    self.update_params()

    self.speed_limit, self.distance, self.source = self._resolve_limit_sources(sm)
    self.speed_limit_offset = self._get_speed_limit_offset()

    self.update_speed_limit_states()

    self.frame += 1
