"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

from cereal import car, custom
from opendbc.car import structs
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.sunnypilot.selfdrive.car.intelligent_cruise_button_management.helpers import get_minimum_set_speed
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.helpers import compare_cluster_target
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import ACTIVE_STATES as SLA_ACTIVE_STATES
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.sla_shm import read_sla_shm

ButtonType = car.CarState.ButtonEvent.Type
SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState

CRUISE_BUTTON_TIMER = {ButtonType.decelCruise: 0, ButtonType.accelCruise: 0,
                       ButtonType.setCruise: 0, ButtonType.resumeCruise: 0,
                       ButtonType.cancel: 0, ButtonType.mainCruise: 0}

V_CRUISE_MIN = 8
V_CRUISE_MAX = 145
V_CRUISE_UNSET = 255


def update_manual_button_timers(CS: car.CarState, button_timers: dict[car.CarState.ButtonEvent.Type, int]) -> None:
  # increment timer for buttons still pressed
  for k in button_timers:
    if button_timers[k] > 0:
      button_timers[k] += 1

  for b in CS.buttonEvents:
    if b.type.raw in button_timers:
      # Start/end timer and store current state on change of button pressed
      button_timers[b.type.raw] = 1 if b.pressed else 0


class VCruiseHelperSP:
  def __init__(self, CP: structs.CarParams, CP_SP: structs.CarParamsSP) -> None:
    self.CP = CP
    self.CP_SP = CP_SP
    self.v_cruise_kph = V_CRUISE_UNSET
    self.v_cruise_cluster_kph = V_CRUISE_UNSET
    self.params = Params()
    self.v_cruise_min = 0
    self.enabled_prev = False

    self.custom_acc_enabled = self.params.get_bool("CustomAccIncrementsEnabled")
    self.short_increment = self.params.get("CustomAccShortPressIncrement", return_default=True)
    self.long_increment = self.params.get("CustomAccLongPressIncrement", return_default=True)

    self.enable_button_timers = CRUISE_BUTTON_TIMER

    # Speed Limit Assist
    self.sla_state = SpeedLimitAssistState.disabled
    self.prev_sla_state = SpeedLimitAssistState.disabled
    self.has_speed_limit = False
    self.speed_limit_final_last = 0.
    self.speed_limit_final_last_kph = 0.
    self.prev_speed_limit_final_last_kph = 0.
    self.sla_ratio = 0.  # FunnyPilot: dynamic offset ratio carried between zones
    self.sla_req_plus = False   # FunnyPilot v3.3.3: preActive arrow direction
    self.sla_req_minus = False
    # FunnyPilot v3.4.0: predictive set-speed ramp. sla_v_cruise_target is the
    # set speed SLA wants the cluster to read right now (0 = no request); we
    # follow it in whole display units, i.e. exactly like tapping +/- on the
    # wheel. _ramp_hold_frames pauses the ramp briefly after a real button
    # press so the driver's own adjustment reaches SLA (which re-derives the
    # offset ratio from it) instead of being overwritten on the next frame.
    self.sla_v_cruise_target = 0.
    self._ramp_hold_frames = 0

  def read_custom_set_speed_params(self) -> None:
    self.custom_acc_enabled = self.params.get_bool("CustomAccIncrementsEnabled")
    self.short_increment = self.params.get("CustomAccShortPressIncrement", return_default=True)
    self.long_increment = self.params.get("CustomAccLongPressIncrement", return_default=True)

  def update_v_cruise_delta(self, long_press: bool, v_cruise_delta: float) -> tuple[bool, float]:
    if not self.custom_acc_enabled:
      v_cruise_delta = v_cruise_delta * (5 if long_press else 1)
      return long_press, v_cruise_delta

    # Apply user-specified multipliers to the base increment
    short_increment = np.clip(self.short_increment, 1, 10)
    long_increment = np.clip(self.long_increment, 1, 10)

    actual_increment = long_increment if long_press else short_increment
    round_to_nearest = actual_increment in (5, 10)
    v_cruise_delta = v_cruise_delta * actual_increment

    return round_to_nearest, v_cruise_delta

  def get_minimum_set_speed(self, is_metric: bool) -> None:
    if self.CP_SP.pcmCruiseSpeed:
      self.v_cruise_min = V_CRUISE_MIN
      return

    self.v_cruise_min = get_minimum_set_speed(is_metric)

  def update_enabled_state(self, CS: car.CarState, enabled: bool) -> bool:
    # special enabled state for non pcmCruiseSpeed, unchanged for non pcmCruise
    if not self.CP_SP.pcmCruiseSpeed:
      update_manual_button_timers(CS, self.enable_button_timers)
      button_pressed = any(self.enable_button_timers[k] > 0 for k in self.enable_button_timers)

      if enabled and not self.enabled_prev:
        self.enabled_prev = not button_pressed
        enabled = False
      elif not enabled:
        self.enabled_prev = enabled

      return enabled and self.enabled_prev

    return enabled

  def update_speed_limit_assist(self, is_metric, LP_SP: custom.LongitudinalPlanSP) -> None:
    resolver = LP_SP.speedLimit.resolver
    self.has_speed_limit = resolver.speedLimitValid or resolver.speedLimitLastValid
    self.speed_limit_final_last = resolver.speedLimitFinalLast
    self.speed_limit_final_last_kph = self.speed_limit_final_last * CV.MS_TO_KPH
    self.sla_state = LP_SP.speedLimit.assist.state
    self.sla_ratio = LP_SP.speedLimit.assist.slaDynamicOffset
    # v3.4.1: ramp target comes over /dev/shm, not capnp (see sla_shm.py — a
    # schema change would force a device rebuild, which this fork avoids).
    # Sampled here (LP_SP rate) rather than per 100 Hz control frame.
    self.sla_v_cruise_target, _ = read_sla_shm()
    self.sla_req_plus, self.sla_req_minus = compare_cluster_target(self.v_cruise_cluster_kph * CV.KPH_TO_MS,
                                                                   self.speed_limit_final_last, is_metric)

  @property
  def update_speed_limit_final_last_changed(self) -> bool:
    return self.has_speed_limit and bool(self.speed_limit_final_last_kph != self.prev_speed_limit_final_last_kph)

  def update_speed_limit_assist_pre_active_confirmed(self, button_type: car.CarState.ButtonEvent.Type, long_press: bool = False) -> bool:
    """FunnyPilot v3.3.3: swallow the SLA preActive confirm press.

    While the activation arrow is showing, a cruise press IN THE ARROW'S
    DIRECTION confirms SLA — it must not also step the set speed, because
    activation ADOPTS the current set speed exactly as it is (SLA derives
    its ratio from it; nothing is written, so there is no jump). Presses in
    the other direction, and long presses, stay ordinary adjustments.
    """
    if long_press:
      return False

    if self.sla_state == SpeedLimitAssistState.preActive or self.prev_sla_state == SpeedLimitAssistState.preActive:
      if button_type == ButtonType.accelCruise and self.sla_req_plus:
        return True
      if button_type == ButtonType.decelCruise and self.sla_req_minus:
        return True

    return False

  # NOTE: CS is deliberately UNANNOTATED. `car.CarState` is a capnp
  # _StructModule, not a Python type, so `car.CarState | None` raises
  # TypeError while the class body is being evaluated at import time — which
  # is exactly what made v3.4.0/v3.4.1 unbootable (manager died importing
  # process_config -> ... -> cruise_ext, so the car sat at the comma splash).
  # Plain `X: car.CarState` annotations elsewhere in this file are fine; it is
  # only the `|` union operator that capnp's module objects don't support.
  def update_speed_limit_assist_v_cruise_non_pcm(self, CS=None) -> None:
    # FunnyPilot: while SLA is active the cluster set speed IS the SLA target.
    # On ACTIVATION nothing is written — the arrow confirm adopts the set
    # speed exactly as it is (no jump). Only on a zone change while ALREADY
    # active does the ratio carry into the new zone: set speed :=
    # new_limit * (1 + ratio). The SLA state machine re-derives the ratio
    # from this exact value, so the snap is idempotent.
    sla_active = self.sla_state in SLA_ACTIVE_STATES and self.prev_sla_state in SLA_ACTIVE_STATES

    if sla_active and self.update_speed_limit_final_last_changed:
      target_kph = self.speed_limit_final_last_kph * (1.0 + self.sla_ratio)
      self.v_cruise_kph = np.clip(round(target_kph, 1), self.v_cruise_min, V_CRUISE_MAX)

    # FunnyPilot v3.4.0: predictive set-speed ramp. Between zone changes,
    # follow SLA's ramped target (speed_limit_assist._update_cruise_ramp) so
    # the SET SPEED itself walks down before a slower zone and up into a
    # faster one — the same thing the driver would do with the cruise
    # buttons. This is why it works under DEC: nothing here depends on which
    # MPC mode is running, only on the set speed every mode already honors.
    # The boundary snap above remains the final authority and is idempotent
    # with the ramp (the ramp has already landed on that value by then).
    if CS is not None and any(b.pressed for b in CS.buttonEvents):
      self._ramp_hold_frames = 100  # ~1 s at the 100 Hz card rate

    if self._ramp_hold_frames > 0:
      self._ramp_hold_frames -= 1
    elif sla_active and self.sla_v_cruise_target > 0.:
      target_kph = self.sla_v_cruise_target * CV.MS_TO_KPH
      self.v_cruise_kph = np.clip(round(target_kph, 1), self.v_cruise_min, V_CRUISE_MAX)

    self.prev_sla_state = self.sla_state
    self.prev_speed_limit_final_last_kph = self.speed_limit_final_last_kph
