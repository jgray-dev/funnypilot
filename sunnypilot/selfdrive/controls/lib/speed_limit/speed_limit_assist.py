"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

FunnyPilot v3.3.3 — Speed Limit Assist: the v3.2.6e speed-domain stack with
the ORIGINAL preActive arrow activation restored, plus pre-zone gas gating.

SLA is a SPEED-DOMAIN feature: it decides the cruise target, never an
acceleration. Braking into a lower zone is owned by the MPC + output shaper
like any other set-speed change. The one accel-adjacent output, the pre-zone
gas gate, is a THROTTLE-ONLY clamp applied in the base planner — it can
coast the car, never brake it.

Activation (the original arrow flow):
  * Arming: with the SpeedLimitMode param set to assist and openpilot long
    engaged, SLA arms. When a speed limit is known — on initial engage or
    whenever the zone changes while inactive — it enters preActive for
    PRE_ACTIVE_WINDOW (6 s). The UI shows an up/down arrow next to the sign:
    up if the set speed is below the limit, down if above.
  * Confirm: pressing the cruise button IN THE ARROW'S DIRECTION during the
    window activates SLA. The confirming tap is swallowed upstream
    (cruise_ext.py) so it doesn't also step the set speed; on activation the
    set speed snaps to the limit and the dynamic ratio starts at 0.
  * If the set speed already equals the limit, SLA activates immediately
    (nothing to confirm). The window simply times out back to inactive
    otherwise, and re-arms on the next zone change.

While active (the v3.2.6e stack, unchanged):
  * The cluster set speed IS the SLA target. Manual cruise adjustments
    re-derive the dynamic offset ratio = (set - limit) / limit (±50% cap).
  * On a zone change the ratio is HELD and cruise_ext moves the set speed to
    limit * (1 + ratio) — +20% carried from a 50 zone puts a 30 zone at 36.
    Because the snap writes exactly limit * (1 + ratio), re-deriving the
    ratio from it is idempotent (no self-wipe at boundaries).
  * Deactivation only on longitudinal disengage or turning the mode off.

Pre-zone gas gating (new in v3.3.3):
  * While active and approaching a LOWER zone, once the remaining distance is
    inside the coast envelope (assumed GATE_COAST_ACCEL natural decel, plus a
    GATE_TIME_BUFFER early-arrival margin), gas_gate_active goes True. The
    base planner then clamps max accel to the measured coast accel — no
    throttle, NO brakes. The resolver no longer switches the limit early, so
    the set-speed snap (and any light braking to shed residual overspeed)
    happens exactly at the boundary.
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
PRE_ACTIVE_WINDOW = 6.0      # secs the activation arrow stays up awaiting the confirm press
LIMIT_SPEED_OFFSET_TH = -1.  # m/s below target -> adapting (display state)
V_CRUISE_UNSET = 255.

RATIO_LIMIT = 0.5  # dynamic offset clamped to +/-50% of the limit

CRUISE_BUTTONS_PLUS = (ButtonType.accelCruise, ButtonType.resumeCruise)
CRUISE_BUTTONS_MINUS = (ButtonType.decelCruise, ButtonType.setCruise)
CONFIRM_HOLD = 0.5  # secs a button release stays consumable by the 20 Hz state machine

# Pre-zone gas gate envelope
GATE_COAST_ACCEL = 0.35   # m/s^2 assumed natural coast decel (drag + engine braking)
GATE_TIME_BUFFER = 1.5    # s — aim to reach the new target this early
GATE_MIN_OVER = 0.3       # m/s — no gate when already at/under the upcoming target


class SpeedLimitAssist:
  def __init__(self, CP: car.CarParams, CP_SP: custom.CarParamsSP, params=None):
    self.CP = CP
    self.CP_SP = CP_SP
    self.params = params if params is not None else (Params() if Params is not None else None)
    self.frame = -1
    self.long_engaged_timer = 0
    self.pre_active_timer = 0

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
    self.gas_gate_active = False

    self.v_cruise_cluster = 0.
    self.v_cruise_cluster_conv = 0
    self.prev_v_cruise_cluster_conv = 0

    self._has_speed_limit = False
    self._had_speed_limit = False
    self._speed_limit = 0.
    self._speed_limit_final_last = 0.
    self.speed_limit_final_last_conv = 0
    self.prev_speed_limit_final_last_conv = 0
    self._next_limit_final = 0.
    self._next_distance = 0.

    self.state = SpeedLimitAssistState.disabled
    self._state_prev = SpeedLimitAssistState.disabled
    self.pcm_op_long = CP.openpilotLongitudinalControl and CP.pcmCruise

    # Dynamic offset ratio: the SLA "value" carried between zones
    self._ratio = 0.0

    # confirm-press detection (fed at carState rate by update_car_state,
    # consumed at 20 Hz): monotonic deadline until which a release is valid
    self._hold_deadline = {"plus": 0., "minus": 0.}

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
    # UI badge: locked == active
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
  def next_zone_target(self) -> float:
    """The target the ratio will produce once the upcoming zone is entered."""
    if self._next_limit_final > 0:
      return self._next_limit_final * (1.0 + self._ratio)
    return 0.

  @property
  def v_cruise_cluster_changed(self) -> bool:
    return bool(self.v_cruise_cluster_conv != self.prev_v_cruise_cluster_conv)

  @property
  def speed_limit_final_last_changed(self) -> bool:
    return bool(self.speed_limit_final_last_conv != self.prev_speed_limit_final_last_conv)

  @property
  def set_speed_matches_limit(self) -> bool:
    return bool(self._base_limit > 0 and self.v_cruise_cluster_conv == self.speed_limit_final_last_conv)

  # ---------- buttons ----------

  def update_car_state(self, CS: car.CarState) -> None:
    """Runs at carState rate (100 Hz) from plannerd; records button releases."""
    now = time.monotonic()
    for b in CS.buttonEvents:
      if b.pressed:
        continue
      if b.type in CRUISE_BUTTONS_PLUS:
        self._hold_deadline["plus"] = now + CONFIRM_HOLD
      elif b.type in CRUISE_BUTTONS_MINUS:
        self._hold_deadline["minus"] = now + CONFIRM_HOLD

  def _consume_release(self, kind: str) -> bool:
    now = time.monotonic()
    if now <= self._hold_deadline[kind]:
      self._hold_deadline[kind] = 0.
      return True
    return False

  def _clear_releases(self) -> None:
    self._hold_deadline = {"plus": 0., "minus": 0.}

  # ---------- state machine ----------

  def _set_ratio_from_cluster(self) -> None:
    if self._base_limit > 0 and self.v_cruise_cluster > 0:
      ratio = (self.v_cruise_cluster - self._base_limit) / self._base_limit
      self._ratio = max(-RATIO_LIMIT, min(RATIO_LIMIT, ratio))

  def _active_or_adapting(self) -> int:
    # compute the offset fresh: the ratio may have been (re)derived this frame
    self.v_offset = self.effective_speed_limit_target - self.v_ego
    return SpeedLimitAssistState.adapting if self.v_offset < LIMIT_SPEED_OFFSET_TH else SpeedLimitAssistState.active

  def _enter_pre_active(self) -> None:
    self.state = SpeedLimitAssistState.preActive
    self.pre_active_timer = int(PRE_ACTIVE_WINDOW / DT_MDL)
    self._clear_releases()

  def _activate(self) -> None:
    # Original-flow activation: the set speed becomes the limit (cruise_ext
    # snaps it on the became-active edge), so the ratio starts at 0.
    self._ratio = 0.0
    self.state = self._active_or_adapting()
    self._clear_releases()

  def _confirm_pressed(self) -> bool:
    """A cruise press in the arrow's direction confirms activation."""
    if self._base_limit <= 0:
      return False
    if self.v_cruise_cluster_conv < self.speed_limit_final_last_conv:
      return self._consume_release("plus")
    if self.v_cruise_cluster_conv > self.speed_limit_final_last_conv:
      return self._consume_release("minus")
    return False

  def update_state_machine(self) -> tuple[bool, bool]:
    self.long_engaged_timer = max(0, self.long_engaged_timer - 1)
    self.pre_active_timer = max(0, self.pre_active_timer - 1)

    if self.state != SpeedLimitAssistState.disabled:
      if not self.long_enabled or not self.enabled:
        self.state = SpeedLimitAssistState.disabled
        self._ratio = 0.0
        self._clear_releases()

      elif self.state in ACTIVE_STATES:
        # Manual adjustment (or our own zone-change snap, which is
        # idempotent): the cluster set speed defines the ratio.
        if self.v_cruise_cluster_changed:
          self._set_ratio_from_cluster()
        self.state = self._active_or_adapting()
        self._clear_releases()  # presses while active are plain speed adjustments

      elif self.state == SpeedLimitAssistState.preActive:
        if self.set_speed_matches_limit or self._confirm_pressed():
          self._activate()
        elif self.speed_limit_final_last_changed and self._has_speed_limit:
          self._enter_pre_active()  # new zone mid-window: restart the window
        elif self.pre_active_timer <= 0 or not self._has_speed_limit:
          self.state = SpeedLimitAssistState.inactive
          self._clear_releases()

      else:  # armed (inactive)
        if self._has_speed_limit and (self.speed_limit_final_last_changed or not self._had_speed_limit):
          # new zone (or first limit seen): offer activation
          self._enter_pre_active()
        elif self.set_speed_matches_limit:
          # dialing the set speed onto the limit activates at any time
          self._activate()

    else:  # DISABLED
      if self.long_enabled and self.enabled:
        if not self.long_enabled_prev:
          self.long_engaged_timer = int(DISABLED_GUARD_PERIOD / DT_MDL)
        elif self.long_engaged_timer <= 0:
          if self._has_speed_limit:
            self._enter_pre_active()
          else:
            self.state = SpeedLimitAssistState.inactive
            self._clear_releases()

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES
    return enabled, active

  # ---------- pre-zone gas gate ----------

  def _update_gas_gate(self) -> None:
    self.gas_gate_active = False
    if not self.is_active:
      return
    next_target = self.next_zone_target
    if next_target <= 0 or self._next_distance <= 0:
      return
    current_target = self.effective_speed_limit_target
    if current_target > 0 and next_target >= current_target:
      return  # not a reduction
    if self.v_ego <= next_target + GATE_MIN_OVER:
      return  # already slow enough
    coast_dist = (self.v_ego ** 2 - next_target ** 2) / (2.0 * GATE_COAST_ACCEL)
    if self._next_distance <= coast_dist + next_target * GATE_TIME_BUFFER:
      self.gas_gate_active = True

  # ---------- events ----------

  def update_events(self, events_sp) -> None:
    if self.state == SpeedLimitAssistState.preActive:
      events_sp.add(EventNameSP.speedLimitPreActive)

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
    # SLA is speed-domain only; published for UI, never for control
    return self.a_ego

  # ---------- main ----------

  def update(self, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, v_cruise_cluster: float, speed_limit: float,
             speed_limit_final_last: float, has_speed_limit: bool, distance: float, events_sp,
             next_speed_limit_final: float = 0., next_distance: float = 0.) -> None:
    self.long_enabled = long_enabled
    self.v_ego = v_ego
    self.a_ego = a_ego

    self._has_speed_limit = has_speed_limit
    self._speed_limit = speed_limit
    self._speed_limit_final_last = speed_limit_final_last
    self._next_limit_final = next_speed_limit_final
    self._next_distance = next_distance

    self.update_params()

    speed_conv = CV.MS_TO_KPH if self.is_metric else CV.MS_TO_MPH
    self.v_cruise_cluster = v_cruise_cluster
    self.v_cruise_cluster_conv = round(self.v_cruise_cluster * speed_conv)
    self.speed_limit_final_last_conv = round(self._speed_limit_final_last * speed_conv)
    self.v_offset = self.effective_speed_limit_target - self.v_ego

    self._state_prev = self.state
    self.is_enabled, self.is_active = self.update_state_machine()

    self._update_gas_gate()
    self.update_events(events_sp)

    self.long_enabled_prev = self.long_enabled
    self._had_speed_limit = self._has_speed_limit
    self.prev_v_cruise_cluster_conv = self.v_cruise_cluster_conv
    self.prev_speed_limit_final_last_conv = self.speed_limit_final_last_conv

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
