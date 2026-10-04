"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from cereal import car
import math

from openpilot.common.constants import CV

from openpilot.sunnypilot.selfdrive.controls.lib.model_settle import ModelSettle, MIN_SETTLE_SECONDS

# Pre-model-gate 3.7.8 used this duration of near-center wheel position.
# It is now an absolute signal-off comfort timeout, with no wheel-angle veto.
FALLBACK_SECONDS = 0.67


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
    self._off_since = None
    self._last_time = None
    self.release_reason = ''
    self.model_settle = ModelSettle()

  def get_params(self) -> None:
    self.enabled = self.params.get_bool("BlinkerPauseLateralControl")
    self.is_metric = self.params.get_bool("IsMetric")
    self.min_speed = self.params.get("BlinkerMinLateralControlSpeed", return_default=True)
    self.reengage_delay = self.params.get("BlinkerLateralReengageDelay", return_default=True)

  def update(self, CS: car.CarState, DT_CTRL: float = 0.01, **model_state) -> bool:
    if not self.enabled:
      self._blinker_was_on = False
      self._off_since = self._last_time = None
      self.blinker_off_timer = 0.0
      self.release_reason = ''
      self.model_settle.reset()
      return False

    one_blinker = CS.leftBlinker != CS.rightBlinker
    speed_factor = CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS
    paused_condition = one_blinker and CS.vEgo < self.min_speed * speed_factor
    if paused_condition:
      self._blinker_was_on = True
      self.release_reason = ''
    if not self._blinker_was_on:
      return False
    # Once paused, crossing the speed threshold cannot unlock a signalled turn.
    # Guiding a curve can keep steeringPressed true even when the model agrees
    # with the driver's path. Judge handover from model/vehicle agreement, not
    # a hands-off interval. Directional EPS/Panda override remains downstream.
    if CS.leftBlinker or CS.rightBlinker:
      self._off_since = self._last_time = None
      self.blinker_off_timer = 0.0
      self.model_settle.reset()
      return True
    now = model_state.get('now')
    if not isinstance(now, (int, float)) or not math.isfinite(now):
      self._off_since = self._last_time = None
      self.model_settle.reset()
      return True
    if self._off_since is None or (self._last_time is not None and now < self._last_time):
      self._off_since = now
      self.model_settle.reset()
    self._last_time = now
    self.blinker_off_timer = now - self._off_since
    state = dict(model_state, speed=CS.vEgo, delay=self.reengage_delay)
    if not self.model_settle.available(**state):
      self.model_settle.reset()
      return True
    early = self.model_settle.update(**state) and self.blinker_off_timer >= max(MIN_SETTLE_SECONDS, self.reengage_delay) - 1e-9
    fallback = self.blinker_off_timer >= max(FALLBACK_SECONDS, self.reengage_delay) - 1e-9
    if early or fallback:
      self._blinker_was_on = False
      self.release_reason = 'model' if early else 'timeout'
      self.model_settle.reset()
    return self._blinker_was_on
