"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

FunnyPilot v3.2.6e — Speed Limit Assist, rewritten for the single-authority
longitudinal architecture. SLA is a SPEED-DOMAIN feature: it decides the
cruise target, never an acceleration. Braking into a lower zone is owned by
the MPC + output shaper like any other set-speed change.

Model: "the cluster set speed IS the SLA target".

  * Arming: with the SpeedLimitMode param set to assist and openpilot long
    engaged, SLA sits silently armed (state = inactive). No prompts.
  * Tap-to-adopt activation: a SHORT tap of the cruise-down button activates
    SLA and ADOPTS the current set speed unchanged. The dynamic offset ratio
    becomes (set_speed - limit) / limit — e.g. set 50 mph in a 45 mph zone
    -> active at +11%, and the car does not change speed at all (the tap is
    swallowed upstream in cruise.py so it doesn't decrement the set speed).
  * Carryover: on a zone change while active, the ratio is HELD and the
    cruise helper (cruise_ext.py) moves the set speed to
    limit * (1 + ratio) — +20% carried from a 50 zone puts a 30 zone at 36.
  * Manual adjustment while active: cruise buttons move the set speed
    normally; SLA recomputes the ratio from the new cluster value
    (36 -> 33 in a 30 zone re-locks the ratio at +10%). Because the zone-
    change snap writes exactly limit * (1 + ratio), recomputing the ratio
    from a snap is idempotent — the ratio can no longer wipe itself at zone
    boundaries (the v0.9.8 bug).
  * Deactivation: only on longitudinal disengage or turning the mode off.

The ratio is based on speed_limit_final_last (posted limit + the user's
configured static offset, which is identity when the offset is off).
"""
import time

from cereal import custom, car
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL

try:
  from openpilot.common.params import Params
except Exception:  # import-light test environments have no compiled params
  Params = None

try:
  from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
except Exception:
  PARAMS_UPDATE_PERIOD = 5.0

from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Mode

ButtonType = car.CarState.ButtonEvent.Type
EventNameSP = custom.OnroadEventSP.EventName
SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState

ACTIVE_STATES = (SpeedLimitAssistState.active, SpeedLimitAssistState.adapting)
ENABLED_STATES = (SpeedLimitAssistState.preActive, SpeedLimitAssistState.pending, *ACTIVE_STATES)

DISABLED_GUARD_PERIOD = 0.5  # secs after long engage before SLA arms
LIMIT_SPEED_OFFSET_TH = -1.  # m/s below target -> adapting (display state)
V_CRUISE_UNSET = 255.

RATIO_LIMIT = 0.5  # dynamic offset clamped to +/-50% of the limit

CRUISE_BUTTONS_PLUS = (ButtonType.accelCruise, ButtonType.resumeCruise)
CRUISE_BUTTONS_MINUS = (ButtonType.decelCruise, ButtonType.setCruise)
TAP_MAX_DURATION = 0.5   # secs; a longer press is a speed adjustment, not an activation
TAP_VALID_WINDOW = 0.5   # secs a released tap stays consumable by the 20 Hz state machine


class SpeedLimitAssist:
  def __init__(self, CP: car.CarParams, CP_SP: custom.CarParamsSP, params=None):
    self.CP = CP
    self.CP_SP = CP_SP
    self.params = params if params is not None else (Params() if Params is not None else None)
    self.frame = -1
    self.long_engaged_timer = 0

    self.is_metric = self._param_bool("IsMetric")
    self._check_availability()
    self.enabled = self._param_mode() == Mode.assist

    self.long_enabled = False
    self.long_enabled_prev = False
    self.is_enabled = False
    self.is_active = False
    self.output_v_target = V_CRUISE_UNSET
    self.output_a_target = 0.
    self.v_ego = 0.
    self.a_ego = 0.
    self.v_offset = 0.

    self.v_cruise_cluster = 0.
    self.v_cruise_cluster_conv = 0
    self.prev_v_cruise_cluster_conv = 0

    self._has_speed_limit = False
    self._speed_limit = 0.
    self._speed_limit_final_last = 0.
    self.speed_limit_final_last_conv = 0
    self.prev_speed_limit_final_last_conv = 0
    self._distance = 0.

    self.state = SpeedLimitAssistState.disabled
    self._state_prev = SpeedLimitAssistState.disabled
    self.pcm_op_long = CP.openpilotLongitudinalControl and CP.pcmCruise

    # Dynamic offset ratio: the SLA "value" carried between zones
    self._ratio = 0.0

    # tap detection (fed at carState rate by update_car_state, consumed at 20 Hz)
    self._press_t: dict[str, float | None] = {"plus": None, "minus": None}
    self._tap_deadline = {"plus": 0., "minus": 0.}

  # ---------- params ----------

  def _param_bool(self, key: str) -> bool:
    return self.params.get_bool(key) if self.params is not None else False

  def _param_mode(self) -> int:
    if self.params is None:
      return int(Mode.assist)
    return self.params.get("SpeedLimitMode", return_default=True)

  def _check_availability(self) -> None:
    if self.params is None:
      return
    try:
      from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.helpers import set_speed_limit_assist_availability
      set_speed_limit_assist_availability(self.CP, self.CP_SP, self.params)
    except Exception:
      pass

  def update_params(self) -> None:
    if self.params is not None and self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.is_metric = self._param_bool("IsMetric")
      self._check_availability()
      self.enabled = self._param_mode() == Mode.assist

  # ---------- published properties ----------

  @property
  def sla_locked(self) -> bool:
    # UI badge: locked == active in the v3.2.6e model
    return self.is_active

  @property
  def dynamic_offset_ratio(self) -> float:
    return self._ratio

  @property
  def dynamic_offset_percent(self) -> float:
    return self._ratio * 100.0

  # ---------- helpers ----------

  @property
  def _base_limit(self) -> float:
    return self._speed_limit_final_last

  @property
  def effective_speed_limit_target(self) -> float:
    if self._base_limit > 0:
      return self._base_limit * (1.0 + self._ratio)
    return 0.

  @property
  def v_cruise_cluster_changed(self) -> bool:
    return bool(self.v_cruise_cluster_conv != self.prev_v_cruise_cluster_conv)

  @property
  def speed_limit_final_last_changed(self) -> bool:
    return bool(self.speed_limit_final_last_conv != self.prev_speed_limit_final_last_conv)

  # ---------- buttons ----------

  def update_car_state(self, CS: car.CarState) -> None:
    """Runs at carState rate (100 Hz) from plannerd; classifies short taps."""
    now = time.monotonic()
    for b in CS.buttonEvents:
      if b.type in CRUISE_BUTTONS_PLUS:
        kind = "plus"
      elif b.type in CRUISE_BUTTONS_MINUS:
        kind = "minus"
      else:
        continue

      if b.pressed:
        self._press_t[kind] = now
      else:
        press_t = self._press_t[kind]
        self._press_t[kind] = None
        # missing press event -> assume tap (some ports only report releases)
        if press_t is None or (now - press_t) <= TAP_MAX_DURATION:
          self._tap_deadline[kind] = now + TAP_VALID_WINDOW

  def _consume_tap(self, kind: str) -> bool:
    now = time.monotonic()
    if now <= self._tap_deadline[kind]:
      self._tap_deadline[kind] = 0.
      return True
    return False

  def _clear_taps(self) -> None:
    self._tap_deadline = {"plus": 0., "minus": 0.}

  # ---------- state machine ----------

  def _set_ratio_from_cluster(self) -> None:
    if self._base_limit > 0 and self.v_cruise_cluster > 0:
      ratio = (self.v_cruise_cluster - self._base_limit) / self._base_limit
      self._ratio = max(-RATIO_LIMIT, min(RATIO_LIMIT, ratio))

  def _active_or_adapting(self) -> int:
    # compute the offset fresh: the ratio may have been (re)derived this frame
    self.v_offset = self.effective_speed_limit_target - self.v_ego
    return SpeedLimitAssistState.adapting if self.v_offset < LIMIT_SPEED_OFFSET_TH else SpeedLimitAssistState.active

  def update_state_machine(self) -> tuple[bool, bool]:
    self.long_engaged_timer = max(0, self.long_engaged_timer - 1)

    if self.state != SpeedLimitAssistState.disabled:
      if not self.long_enabled or not self.enabled:
        self.state = SpeedLimitAssistState.disabled
        self._ratio = 0.0
        self._clear_taps()

      elif self.state in ACTIVE_STATES:
        # Manual adjustment (or our own zone-change snap, which is
        # idempotent): the cluster set speed defines the ratio.
        if self.v_cruise_cluster_changed:
          self._set_ratio_from_cluster()
        self.state = self._active_or_adapting()
        self._clear_taps()  # taps while active are plain speed adjustments

      else:  # armed (inactive)
        if self._consume_tap("minus") and self._base_limit > 0:
          # Tap-to-adopt: keep the current set speed, derive the ratio from it
          self._set_ratio_from_cluster()
          self.state = self._active_or_adapting()

    else:  # DISABLED
      if self.long_enabled and self.enabled:
        if not self.long_enabled_prev:
          self.long_engaged_timer = int(DISABLED_GUARD_PERIOD / DT_MDL)
        elif self.long_engaged_timer <= 0:
          self.state = SpeedLimitAssistState.inactive
          self._clear_taps()

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES
    return enabled, active

  # ---------- events ----------

  def update_events(self, events_sp) -> None:
    if self.is_active:
      if self._state_prev not in ACTIVE_STATES:
        events_sp.add(EventNameSP.speedLimitActive)
      elif self.speed_limit_final_last_changed and self._has_speed_limit:
        events_sp.add(EventNameSP.speedLimitChanged)

  # ---------- outputs ----------

  def get_v_target_from_control(self) -> float:
    if self.is_active and self._base_limit > 0:
      return self.effective_speed_limit_target
    return V_CRUISE_UNSET

  def get_a_target_from_control(self) -> float:
    # v3.2.6e: SLA is speed-domain only; published for UI, never for control
    return self.a_ego

  # ---------- main ----------

  def update(self, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, v_cruise_cluster: float, speed_limit: float,
             speed_limit_final_last: float, has_speed_limit: bool, distance: float, events_sp) -> None:
    self.long_enabled = long_enabled
    self.v_ego = v_ego
    self.a_ego = a_ego

    self._has_speed_limit = has_speed_limit
    self._speed_limit = speed_limit
    self._speed_limit_final_last = speed_limit_final_last
    self._distance = distance

    self.update_params()

    speed_conv = CV.MS_TO_KPH if self.is_metric else CV.MS_TO_MPH
    self.v_cruise_cluster = v_cruise_cluster
    self.v_cruise_cluster_conv = round(self.v_cruise_cluster * speed_conv)
    self.speed_limit_final_last_conv = round(self._speed_limit_final_last * speed_conv)
    self.v_offset = self.effective_speed_limit_target - self.v_ego

    self._state_prev = self.state
    self.is_enabled, self.is_active = self.update_state_machine()

    self.update_events(events_sp)

    self.long_enabled_prev = self.long_enabled
    self.prev_v_cruise_cluster_conv = self.v_cruise_cluster_conv
    self.prev_speed_limit_final_last_conv = self.speed_limit_final_last_conv

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
