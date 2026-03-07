"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import time

from cereal import custom, car
from openpilot.common.params import Params
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit import PCM_LONG_REQUIRED_MAX_SET_SPEED, CONFIRM_SPEED_THRESHOLD
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Mode
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.helpers import compare_cluster_target, set_speed_limit_assist_availability

ButtonType = car.CarState.ButtonEvent.Type
EventNameSP = custom.OnroadEventSP.EventName
SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState
SpeedLimitSource = custom.LongitudinalPlanSP.SpeedLimit.Source

ACTIVE_STATES = (SpeedLimitAssistState.active, SpeedLimitAssistState.adapting)
ENABLED_STATES = (SpeedLimitAssistState.preActive, SpeedLimitAssistState.pending, *ACTIVE_STATES)

DISABLED_GUARD_PERIOD = 0.5  # secs.
# secs. Time to wait after activation before considering temp deactivation signal.
PRE_ACTIVE_GUARD_PERIOD = {
  True: 15,
  False: 5,
}
SPEED_LIMIT_CHANGED_HOLD_PERIOD = 1  # secs. Time to wait after speed limit change before switching to preActive.

LIMIT_MIN_ACC = -1.5  # m/s^2 Maximum deceleration allowed for limit controllers to provide.
LIMIT_MAX_ACC = 1.0  # m/s^2 Maximum acceleration allowed for limit controllers to provide while active.
LIMIT_MIN_SPEED = 8.33  # m/s, Minimum speed limit to provide as solution on limit controllers.
LIMIT_SPEED_OFFSET_TH = -1.0  # m/s Maximum offset between speed limit and current speed for adapting state.
V_CRUISE_UNSET = 255.0

CRUISE_BUTTONS_PLUS = (ButtonType.accelCruise, ButtonType.resumeCruise)
CRUISE_BUTTONS_MINUS = (ButtonType.decelCruise, ButtonType.setCruise)
CRUISE_BUTTON_CONFIRM_HOLD = 0.5  # secs.


class SpeedLimitAssist:
  _speed_limit_final_last: float
  _distance: float
  v_ego: float
  a_ego: float
  v_offset: float

  def __init__(self, CP: car.CarParams, CP_SP: custom.CarParamsSP):
    self.params = Params()
    self.CP = CP
    self.CP_SP = CP_SP
    self.frame = -1
    self.long_engaged_timer = 0
    self.pre_active_timer = 0
    self.is_metric = self.params.get_bool("IsMetric")
    set_speed_limit_assist_availability(self.CP, self.CP_SP, self.params)
    self.enabled = self.params.get("SpeedLimitMode", return_default=True) == Mode.assist
    self.long_enabled = False
    self.long_enabled_prev = False
    self.is_enabled = False
    self.is_active = False
    self.output_v_target = V_CRUISE_UNSET
    self.output_a_target = 0.0
    self.v_ego = 0.0
    self.a_ego = 0.0
    self.v_offset = 0.0
    self.target_set_speed_conv = 0
    self.prev_target_set_speed_conv = 0
    self.v_cruise_cluster = 0.0
    self.v_cruise_cluster_prev = 0.0
    self.v_cruise_cluster_conv = 0
    self.prev_v_cruise_cluster_conv = 0
    self._has_speed_limit = False
    self._speed_limit = 0.0
    self._speed_limit_final_last = 0.0
    self.speed_limit_prev = 0.0
    self.speed_limit_final_last_conv = 0
    self.prev_speed_limit_final_last_conv = 0
    self._distance = 0.0
    self.state = SpeedLimitAssistState.disabled
    self._state_prev = SpeedLimitAssistState.disabled
    self.pcm_op_long = CP.openpilotLongitudinalControl and CP.pcmCruise

    self._plus_hold = 0.0
    self._minus_hold = 0.0
    self._last_carstate_ts = 0.0
    self._last_user_cruise_change_ts = 0.0

    # FunnyPilot: Dynamic SLA locking
    self._sla_locked = False  # True once SLA has been activated; cleared only on disable
    self._dynamic_offset_ratio = 0.0  # (v_cruise - speed_limit_final_last) / speed_limit_final_last

    # TODO-SP: SLA's own output_a_target for planner
    # Solution functions mapped to respective states
    self.acceleration_solutions = {
      SpeedLimitAssistState.disabled: self.get_current_acceleration_as_target,
      SpeedLimitAssistState.inactive: self.get_current_acceleration_as_target,
      SpeedLimitAssistState.preActive: self.get_current_acceleration_as_target,
      SpeedLimitAssistState.pending: self.get_current_acceleration_as_target,
      SpeedLimitAssistState.adapting: self.get_adapting_state_target_acceleration,
      SpeedLimitAssistState.active: self.get_active_state_target_acceleration,
    }

  @property
  def speed_limit_changed(self) -> bool:
    return self._has_speed_limit and bool(self._speed_limit != self.speed_limit_prev)

  @property
  def v_cruise_cluster_changed(self) -> bool:
    return bool(self.v_cruise_cluster_conv != self.prev_v_cruise_cluster_conv)

  @property
  def target_set_speed_confirmed(self) -> bool:
    return bool(self.v_cruise_cluster_conv == self.target_set_speed_conv)

  @property
  def v_cruise_cluster_below_confirm_speed_threshold(self) -> bool:
    return bool(self.v_cruise_cluster_conv < CONFIRM_SPEED_THRESHOLD[self.is_metric])

  @property
  def sla_locked(self) -> bool:
    return self._sla_locked

  @property
  def dynamic_offset_ratio(self) -> float:
    return self._dynamic_offset_ratio

  @property
  def dynamic_offset_percent(self) -> float:
    return self._dynamic_offset_ratio * 100.0

  def _effective_speed_limit_target(self) -> float:
    """FunnyPilot: Returns the effective speed target accounting for dynamic offset when locked."""
    if self._sla_locked and self._has_speed_limit and self._speed_limit_final_last > 0:
      return self._speed_limit_final_last * (1.0 + self._dynamic_offset_ratio)
    return self._speed_limit_final_last

  def _update_locked_offset(self) -> None:
    """FunnyPilot: Recalculate dynamic offset when user adjusts cruise while locked."""
    # Only update if the user recently pressed a cruise adjustment button (within last 3 seconds)
    if time.monotonic() - self._last_user_cruise_change_ts > 3.0:
      return

    if self._has_speed_limit and self._speed_limit_final_last > 0 and self.v_cruise_cluster > 0:
      ratio = (self.v_cruise_cluster - self._speed_limit_final_last) / self._speed_limit_final_last
      # Cap to ±50% to prevent extreme effective targets from bad data
      self._dynamic_offset_ratio = max(-0.5, min(0.5, ratio))

  def update_active_event(self, events_sp: EventsSP) -> None:
    if self.v_cruise_cluster_below_confirm_speed_threshold:
      events_sp.add(EventNameSP.speedLimitChanged)
    else:
      events_sp.add(EventNameSP.speedLimitActive)

  def get_v_target_from_control(self) -> float:
    if self._has_speed_limit:
      effective_target = self._effective_speed_limit_target()
      if self.pcm_op_long and self.is_enabled:
        return effective_target
      if not self.pcm_op_long and self.is_active:
        return effective_target

    # Fallback
    return V_CRUISE_UNSET

  # TODO-SP: SLA's own output_a_target for planner
  def get_a_target_from_control(self) -> float:
    # FunnyPilot: Apply gas gating for speed limit reductions
    if self.should_cut_gas_for_speed_limit():
      return min(self.a_ego, 0.0)  # Cut gas, no positive acceleration
    return self.a_ego

  def update_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.is_metric = self.params.get_bool("IsMetric")
      set_speed_limit_assist_availability(self.CP, self.CP_SP, self.params)
      self.enabled = self.params.get("SpeedLimitMode", return_default=True) == Mode.assist

  def update_car_state(self, CS: car.CarState) -> None:
    now = time.monotonic()
    self._last_carstate_ts = now

    for b in CS.buttonEvents:
      if b.pressed:
        if b.type in CRUISE_BUTTONS_PLUS:
          self._plus_hold = max(self._plus_hold, now + CRUISE_BUTTON_CONFIRM_HOLD)
          self._last_user_cruise_change_ts = now
        elif b.type in CRUISE_BUTTONS_MINUS:
          self._minus_hold = max(self._minus_hold, now + CRUISE_BUTTON_CONFIRM_HOLD)
          self._last_user_cruise_change_ts = now

  def _get_button_release(self, req_plus: bool, req_minus: bool) -> bool:
    now = time.monotonic()
    if req_plus and now <= self._plus_hold:
      self._plus_hold = 0.0
      return True
    elif req_minus and now <= self._minus_hold:
      self._minus_hold = 0.0
      return True

    # expired
    if now > self._plus_hold:
      self._plus_hold = 0.0
    if now > self._minus_hold:
      self._minus_hold = 0.0
    return False

  def update_calculations(self, v_cruise_cluster: float) -> None:
    speed_conv = CV.MS_TO_KPH if self.is_metric else CV.MS_TO_MPH
    self.v_cruise_cluster = v_cruise_cluster
    effective_target = self._effective_speed_limit_target()

    # FunnyPilot: Compute v_offset against effective target (accounts for dynamic offset when locked)
    self.v_offset = effective_target - self.v_ego

    self.speed_limit_final_last_conv = round(effective_target * speed_conv)
    self.v_cruise_cluster_conv = round(self.v_cruise_cluster * speed_conv)

    cst_low, cst_high = PCM_LONG_REQUIRED_MAX_SET_SPEED[self.is_metric]
    pcm_long_required_max = cst_low if self._has_speed_limit and self.speed_limit_final_last_conv < CONFIRM_SPEED_THRESHOLD[self.is_metric] else cst_high
    pcm_long_required_max_set_speed_conv = round(pcm_long_required_max * speed_conv)

    self.target_set_speed_conv = pcm_long_required_max_set_speed_conv if self.pcm_op_long else self.speed_limit_final_last_conv

  @property
  def apply_confirm_speed_threshold(self) -> bool:
    # below CST: always require user confirmation
    if self.v_cruise_cluster_below_confirm_speed_threshold:
      return True

    # at/above CST:
    # - new speed limit >= CST: auto change
    # - new speed limit < CST: user confirmation required
    return bool(self.speed_limit_final_last_conv < CONFIRM_SPEED_THRESHOLD[self.is_metric])

  def get_current_acceleration_as_target(self) -> float:
    return self.a_ego

  def get_adapting_state_target_acceleration(self) -> float:
    effective_target = self._effective_speed_limit_target()
    if self._distance > 0:
      return (effective_target**2 - self.v_ego**2) / (2.0 * self._distance)

    return self.v_offset / float(ModelConstants.T_IDXS[CONTROL_N])

  def get_active_state_target_acceleration(self) -> float:
    return self.v_offset / float(ModelConstants.T_IDXS[CONTROL_N])

  def should_cut_gas_for_speed_limit(self) -> bool:
    """FunnyPilot: Early gas gating for speed limit reductions - coast to new speed by the time we hit the limit"""
    if not self.long_enabled or not self._has_speed_limit:
      return False

    effective_target = self._effective_speed_limit_target()

    # Only for speed reductions (with offset applied)
    speed_diff = self.v_ego - effective_target
    if speed_diff <= 0:
      return False

    # Only apply if we have a valid distance to the speed limit ahead
    if self._distance <= 0:
      return False

    # Calculate coast distance needed to reach target speed
    # Using coast deceleration of -0.15 m/s² (gentle engine braking + drag)
    coast_decel = -0.15
    coast_distance_needed = (effective_target**2 - self.v_ego**2) / (2.0 * coast_decel)

    # Add buffer: we want to reach target speed slightly BEFORE the limit
    # Buffer = 2 seconds of travel at new speed limit
    buffer_distance = effective_target * 2.0
    total_distance_needed = coast_distance_needed + buffer_distance

    # Cut gas if we're within the coast distance
    return self._distance <= total_distance_needed

  def _update_confirmed_state(self):
    if self._has_speed_limit:
      if self.v_offset < LIMIT_SPEED_OFFSET_TH:
        self.state = SpeedLimitAssistState.adapting
      else:
        self.state = SpeedLimitAssistState.active
      self._sla_locked = True  # FunnyPilot: Lock into dynamic SLA mode on activation
    else:
      self.state = SpeedLimitAssistState.pending

  def _update_non_pcm_long_confirmed_state(self) -> bool:
    if self.target_set_speed_confirmed:
      return True

    if self.state != SpeedLimitAssistState.preActive:
      return False

    req_plus, req_minus = compare_cluster_target(self.v_cruise_cluster, self._effective_speed_limit_target(), self.is_metric)

    return self._get_button_release(req_plus, req_minus)

  def update_state_machine_pcm_op_long(self):
    self.long_engaged_timer = max(0, self.long_engaged_timer - 1)
    self.pre_active_timer = max(0, self.pre_active_timer - 1)

    # ACTIVE, ADAPTING, PENDING, PRE_ACTIVE, INACTIVE
    if self.state != SpeedLimitAssistState.disabled:
      if not self.long_enabled or not self.enabled:
        self.state = SpeedLimitAssistState.disabled
        # FunnyPilot: Clear dynamic lock on full disable
        self._sla_locked = False
        self._dynamic_offset_ratio = 0.0

      else:
        # ACTIVE
        if self.state == SpeedLimitAssistState.active:
          if self.v_cruise_cluster_changed:
            if self._sla_locked:
              # FunnyPilot: Dynamic SLA - update offset ratio, stay active
              self._update_locked_offset()
            else:
              # FunnyPilot: User manually changed speed -> deactivate SLA
              self.state = SpeedLimitAssistState.inactive
          elif self.speed_limit_changed:
            # FunnyPilot: Auto-track speed limit changes - no user confirmation required
            self._update_confirmed_state()
          elif self._has_speed_limit and self.v_offset < LIMIT_SPEED_OFFSET_TH:
            self.state = SpeedLimitAssistState.adapting

        # ADAPTING
        elif self.state == SpeedLimitAssistState.adapting:
          if self.v_cruise_cluster_changed:
            if self._sla_locked:
              # FunnyPilot: Dynamic SLA - update offset ratio, stay adapting
              self._update_locked_offset()
            else:
              # FunnyPilot: User manually changed speed -> deactivate SLA
              self.state = SpeedLimitAssistState.inactive
          elif self.speed_limit_changed:
            # FunnyPilot: Auto-track speed limit changes
            self._update_confirmed_state()
          elif self.v_offset >= LIMIT_SPEED_OFFSET_TH:
            self.state = SpeedLimitAssistState.active

        # PENDING
        elif self.state == SpeedLimitAssistState.pending:
          if self.target_set_speed_confirmed:
            self._update_confirmed_state()
          elif self.speed_limit_changed:
            if self._sla_locked:
              # FunnyPilot: Already locked - auto-activate on new limit
              self._update_confirmed_state()
            else:
              self.state = SpeedLimitAssistState.preActive
              self.pre_active_timer = int(PRE_ACTIVE_GUARD_PERIOD[self.pcm_op_long] / DT_MDL)

        # PRE_ACTIVE
        elif self.state == SpeedLimitAssistState.preActive:
          if self.target_set_speed_confirmed:
            self._update_confirmed_state()
          elif self.pre_active_timer <= 0:
            # Timeout - session ended
            self.state = SpeedLimitAssistState.inactive

        # INACTIVE
        elif self.state == SpeedLimitAssistState.inactive:
          if self._sla_locked and self.speed_limit_changed and self._has_speed_limit:
            # FunnyPilot: Auto-reactivate on new zone when locked
            self._update_confirmed_state()
          elif not self._sla_locked and self.speed_limit_changed and self._has_speed_limit:
            # FunnyPilot: Re-prompt when entering a new speed limit zone
            self.state = SpeedLimitAssistState.preActive
            self.pre_active_timer = int(PRE_ACTIVE_GUARD_PERIOD[self.pcm_op_long] / DT_MDL)

    # DISABLED
    elif self.state == SpeedLimitAssistState.disabled:
      if self.long_enabled and self.enabled:
        # start or reset preActive timer if initially enabled or manual set speed change detected
        if not self.long_enabled_prev or self.v_cruise_cluster_changed:
          self.long_engaged_timer = int(DISABLED_GUARD_PERIOD / DT_MDL)

        elif self.long_engaged_timer <= 0:
          if self.target_set_speed_confirmed:
            self._update_confirmed_state()
          elif self._has_speed_limit:
            self.state = SpeedLimitAssistState.preActive
            self.pre_active_timer = int(PRE_ACTIVE_GUARD_PERIOD[self.pcm_op_long] / DT_MDL)
          else:
            self.state = SpeedLimitAssistState.pending

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES

    return enabled, active

  def update_state_machine_non_pcm_long(self):
    self.long_engaged_timer = max(0, self.long_engaged_timer - 1)
    self.pre_active_timer = max(0, self.pre_active_timer - 1)

    # ACTIVE, ADAPTING, PENDING, PRE_ACTIVE, INACTIVE
    if self.state != SpeedLimitAssistState.disabled:
      if not self.long_enabled or not self.enabled:
        self.state = SpeedLimitAssistState.disabled
        # FunnyPilot: Clear dynamic lock on full disable
        self._sla_locked = False
        self._dynamic_offset_ratio = 0.0

      else:
        # ACTIVE
        if self.state == SpeedLimitAssistState.active:
          if self.v_cruise_cluster_changed:
            if self._sla_locked:
              # FunnyPilot: Dynamic SLA - update offset ratio, stay active
              self._update_locked_offset()
            else:
              # FunnyPilot: User manually changed speed -> deactivate SLA
              self.state = SpeedLimitAssistState.inactive
          elif self.speed_limit_changed:
            # FunnyPilot: Auto-track speed limit changes - no confirmation required
            self.state = SpeedLimitAssistState.active  # stay active, speed_limit_final_last auto-updates

        # PRE_ACTIVE
        elif self.state == SpeedLimitAssistState.preActive:
          if self._update_non_pcm_long_confirmed_state():
            self.state = SpeedLimitAssistState.active
            self._sla_locked = True  # FunnyPilot: Lock on activation
          elif self.pre_active_timer <= 0:
            # Timeout - session ended
            self.state = SpeedLimitAssistState.inactive

        # INACTIVE
        elif self.state == SpeedLimitAssistState.inactive:
          if self._sla_locked and self.speed_limit_changed and self._has_speed_limit:
            # FunnyPilot: Auto-reactivate on new zone when locked
            self.state = SpeedLimitAssistState.active
          elif not self._sla_locked:
            # FunnyPilot: Re-prompt when entering a new speed limit zone
            if self.speed_limit_changed and self._has_speed_limit:
              self.state = SpeedLimitAssistState.preActive
              self.pre_active_timer = int(PRE_ACTIVE_GUARD_PERIOD[self.pcm_op_long] / DT_MDL)
            elif self._update_non_pcm_long_confirmed_state():
              self.state = SpeedLimitAssistState.active
              self._sla_locked = True  # FunnyPilot: Lock on activation

    # DISABLED
    elif self.state == SpeedLimitAssistState.disabled:
      if self.long_enabled and self.enabled:
        # start or reset preActive timer if initially enabled or manual set speed change detected
        if not self.long_enabled_prev or self.v_cruise_cluster_changed:
          self.long_engaged_timer = int(DISABLED_GUARD_PERIOD / DT_MDL)

        elif self.long_engaged_timer <= 0:
          if self._update_non_pcm_long_confirmed_state():
            self.state = SpeedLimitAssistState.active
            self._sla_locked = True  # FunnyPilot: Lock on first activation
          elif self._has_speed_limit:
            self.state = SpeedLimitAssistState.preActive
            self.pre_active_timer = int(PRE_ACTIVE_GUARD_PERIOD[self.pcm_op_long] / DT_MDL)
          else:
            self.state = SpeedLimitAssistState.inactive

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES

    return enabled, active

  def update_events(self, events_sp: EventsSP) -> None:
    if self.state == SpeedLimitAssistState.preActive:
      events_sp.add(EventNameSP.speedLimitPreActive)

    if self.state == SpeedLimitAssistState.pending and self._state_prev != SpeedLimitAssistState.pending:
      events_sp.add(EventNameSP.speedLimitPending)

    if self.is_active:
      if self._state_prev not in ACTIVE_STATES:
        self.update_active_event(events_sp)

      # only notify if we acquire a valid speed limit
      # do not check has_speed_limit here
      elif self._speed_limit != self.speed_limit_prev:
        if self.speed_limit_prev <= 0:
          self.update_active_event(events_sp)
        elif self.speed_limit_prev > 0 and self._speed_limit > 0:
          self.update_active_event(events_sp)

  def update(
    self,
    long_enabled: bool,
    long_override: bool,
    v_ego: float,
    a_ego: float,
    v_cruise_cluster: float,
    speed_limit: float,
    speed_limit_final_last: float,
    has_speed_limit: bool,
    distance: float,
    events_sp: EventsSP,
  ) -> None:
    self.long_enabled = long_enabled
    self.v_ego = v_ego
    self.a_ego = a_ego

    self._has_speed_limit = has_speed_limit
    self._speed_limit = speed_limit
    self._speed_limit_final_last = speed_limit_final_last
    self._distance = distance

    self.update_params()
    self.update_calculations(v_cruise_cluster)

    self._state_prev = self.state
    if self.pcm_op_long:
      self.is_enabled, self.is_active = self.update_state_machine_pcm_op_long()
    else:
      self.is_enabled, self.is_active = self.update_state_machine_non_pcm_long()

    self.update_events(events_sp)

    # Update change tracking variables
    self.speed_limit_prev = self._speed_limit
    self.v_cruise_cluster_prev = self.v_cruise_cluster
    self.long_enabled_prev = self.long_enabled
    self.prev_target_set_speed_conv = self.target_set_speed_conv
    self.prev_v_cruise_cluster_conv = self.v_cruise_cluster_conv
    self.prev_speed_limit_final_last_conv = self.speed_limit_final_last_conv

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
