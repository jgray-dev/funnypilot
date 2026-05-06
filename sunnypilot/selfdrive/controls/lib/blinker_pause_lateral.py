"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from cereal import car

from openpilot.common.constants import CV
from openpilot.common.params import Params

# v2.0.1 — port of the 1.0.8.3 post-blinker unwind feature. Lateral control
# stays paused after the blinker turns off until the steering wheel comes
# back within UNWIND_THRESHOLD_DEG of center. Hardcoded on for this personal
# branch (no Params toggle — params_keys.h whitelist forbids new params).
UNWIND_THRESHOLD_DEG = 20.0
UNWIND_MODE = True


class BlinkerPauseLateral:
  def __init__(self):
    self.params = Params()

    self.enabled = self.params.get_bool("BlinkerPauseLateralControl")
    self.is_metric = self.params.get_bool("IsMetric")
    self.min_speed = 0
    self.reengage_delay = 0
    self.blinker_off_timer = 0.0
    self._blinker_was_on = False

  def get_params(self) -> None:
    self.enabled = self.params.get_bool("BlinkerPauseLateralControl")
    self.is_metric = self.params.get_bool("IsMetric")
    self.min_speed = self.params.get("BlinkerMinLateralControlSpeed", return_default=True)
    self.reengage_delay = self.params.get("BlinkerLateralReengageDelay", return_default=True)

  def update(self, CS: car.CarState, DT_CTRL: float = 0.01) -> bool:
    if not self.enabled:
      return False

    one_blinker = CS.leftBlinker != CS.rightBlinker
    speed_factor = CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS
    min_speed_ms = self.min_speed * speed_factor
    below_speed = CS.vEgo < min_speed_ms
    paused_condition = one_blinker and below_speed

    if UNWIND_MODE:
      if paused_condition:
        self._blinker_was_on = True
      elif self._blinker_was_on:
        # Blinker just turned off — stay paused until wheel returns near center
        if abs(CS.steeringAngleDeg) < UNWIND_THRESHOLD_DEG:
          self._blinker_was_on = False
      return bool(paused_condition or self._blinker_was_on)

    if paused_condition:
      self.blinker_off_timer = self.reengage_delay
    elif self.blinker_off_timer > 0:
      self.blinker_off_timer -= DT_CTRL
    return bool(paused_condition or self.blinker_off_timer > 0)
