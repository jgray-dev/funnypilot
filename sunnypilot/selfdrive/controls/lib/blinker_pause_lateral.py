"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from cereal import car

from openpilot.common.constants import CV

from openpilot.sunnypilot.selfdrive.controls.lib.model_settle import ModelSettle


class BlinkerPauseLateral:
  def __init__(self, params=None):
    if params is None:
      from openpilot.common.params import Params
      params = Params()
    self.params = params

    self.enabled = self.params.get_bool("BlinkerPauseLateralControl")
    self.is_metric = self.params.get_bool("IsMetric")
    self.min_speed = 0
    self.reengage_delay = 0
    self.blinker_off_timer = 0.0
    self._blinker_was_on = False
    self.model_settle = ModelSettle()

  def get_params(self) -> None:
    self.enabled = self.params.get_bool("BlinkerPauseLateralControl")
    self.is_metric = self.params.get_bool("IsMetric")
    self.min_speed = self.params.get("BlinkerMinLateralControlSpeed", return_default=True)
    self.reengage_delay = self.params.get("BlinkerLateralReengageDelay", return_default=True)

  def update(self, CS: car.CarState, DT_CTRL: float = 0.01, **model_state) -> bool:
    if not self.enabled:
      self._blinker_was_on = False
      self.model_settle.reset()
      return False

    one_blinker = CS.leftBlinker != CS.rightBlinker
    speed_factor = CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS
    paused_condition = one_blinker and CS.vEgo < self.min_speed * speed_factor
    if paused_condition:
      self._blinker_was_on = True
    if not self._blinker_was_on:
      return False
    # Once paused, crossing the speed threshold cannot unlock a signalled turn.
    if CS.leftBlinker or CS.rightBlinker or CS.steeringPressed or not model_state:
      self.model_settle.reset()
      return True
    if self.model_settle.update(speed=CS.vEgo, delay=self.reengage_delay, **model_state):
      self._blinker_was_on = False
      self.model_settle.reset()
    return self._blinker_was_on
