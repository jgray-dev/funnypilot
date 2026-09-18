"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from abc import abstractmethod, ABC
import time

import cereal.messaging as messaging
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET

MAX_SPEED_LIMIT = V_CRUISE_UNSET * CV.KPH_TO_MS
LOCATION_MAX_AGE = 2.0


class BaseMapData(ABC):
  def __init__(self):
    self.sm = messaging.SubMaster(['liveLocationKalman'])
    self.pm = messaging.PubMaster(['liveMapDataSP'])

    self.localizer_valid = False
    self.last_bearing = None
    # Start with a fresh localizer fix, never a previous drive's disk value.
    self.last_position = None

  def location_is_fresh(self) -> bool:
    received = self.sm.recv_time['liveLocationKalman']
    age = time.monotonic() - received
    return bool(received > 0 and 0 <= age <= LOCATION_MAX_AGE and self.sm.valid['liveLocationKalman'])

  @abstractmethod
  def update_location(self) -> None:
    pass

  @abstractmethod
  def get_current_speed_limit(self) -> float:
    pass

  @abstractmethod
  def get_next_speed_limit_and_distance(self) -> tuple[float, float]:
    pass

  @abstractmethod
  def get_current_road_name(self) -> str:
    pass

  def publish(self) -> None:
    speed_limit = self.get_current_speed_limit()
    next_speed_limit, next_speed_limit_distance = self.get_next_speed_limit_and_distance()

    mapd_sp_send = messaging.new_message('liveMapDataSP')
    mapd_sp_send.valid = self.localizer_valid
    live_map_data = mapd_sp_send.liveMapDataSP

    live_map_data.speedLimitValid = bool(self.localizer_valid and MAX_SPEED_LIMIT > speed_limit > 0)
    live_map_data.speedLimit = speed_limit
    live_map_data.speedLimitAheadValid = bool(self.localizer_valid and MAX_SPEED_LIMIT > next_speed_limit > 0)
    live_map_data.speedLimitAhead = next_speed_limit
    live_map_data.speedLimitAheadDistance = next_speed_limit_distance
    live_map_data.roadName = self.get_current_road_name()

    self.pm.send('liveMapDataSP', mapd_sp_send)

  def tick(self) -> None:
    self.sm.update(0)
    self.update_location()
    self.publish()
