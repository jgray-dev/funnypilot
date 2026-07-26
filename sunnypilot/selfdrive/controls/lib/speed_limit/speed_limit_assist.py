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
    (cruise_ext.py) so it doesn't step the set speed — activation ADOPTS the
    current set speed unchanged (no jump, no jerk) and derives the dynamic
    ratio from it: 50 set in a 45 zone activates at +11%, still doing 50.
  * If the set speed already equals the limit, SLA activates immediately
    (nothing to confirm, ratio 0). The window simply times out back to
    inactive otherwise, and re-arms on the next zone change.

While active (the v3.2.6e stack, unchanged):
  * The cluster set speed IS the SLA target. Manual cruise adjustments
    re-derive the dynamic offset ratio = (set - limit) / limit (±50% cap).
  * On a zone change the ratio is HELD and cruise_ext moves the set speed to
    limit * (1 + ratio) — +20% carried from a 50 zone puts a 30 zone at 36.
    Because the snap writes exactly limit * (1 + ratio), re-deriving the
    ratio from it is idempotent (no self-wipe at boundaries).
  * Deactivation only on longitudinal disengage or turning the mode off.

Predictive set-speed ramp + gas gate (v3.4.5):
  * Approaching a LOWER zone, the SET SPEED itself is walked down before the
    boundary on a constant-decel envelope, so the car arrives at the sign
    already at the new limit instead of stepping there and then hauling the
    speed off. See _update_cruise_ramp for the full derivation; the rate is
    ~1 mph/s, tightening only as far as the MPC's own CRUISE_MIN_ACCEL allows.
  * gas_gate_active is now defined off that ramp: True exactly while the ramp
    is holding the set speed below the current zone's target. The base planner
    then clamps max accel to the coast accel — no throttle, NO brakes.

  * NOTE ON HISTORY: through v3.4.4 neither of these ever ran on a moving car.
    Both keyed off `distance_to_next_limit`, which the resolver was computing
    with a monotonic-minus-epoch subtraction and returning as ~4e10 m. The
    boundary behaviour the driver felt was purely the old set-speed slew cap
    firing after the zone changed. Fixed in speed_limit_resolver.py.
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

# How many 20 Hz frames a cruise button event keeps explaining cluster changes.
# Must comfortably exceed the plannerd -> card -> carState round trip, so the
# change a press caused is still attributed to that press when the state machine
# sees it. Counted in FRAMES, not wall-clock seconds, deliberately: the window
# has to be a fixed number of update() calls to be reasoned about (and tested)
# at all — a monotonic deadline makes the window's length depend on scheduling.
BUTTON_INTENT_FRAMES = int(0.5 / DT_MDL)  # 10 frames = 0.5 s

# FunnyPilot v3.4.5 — predictive SET SPEED ramp (see _update_cruise_ramp).
#
# WHY THE SET SPEED AND NOT AN MPC CAP: the v3.3.9 attempt shaped an internal
# planner value and did essentially nothing under DEC. The cruise set speed is
# the one quantity every mode honors (acc: cruise obstacle; blended: position
# cap) AND the one the driver can see on the cluster, so walking it is both
# effective and legible — it looks exactly like taps on the wheel.
#
# THE RATE, derived rather than picked. The user asked for the adjustment to be
# spread over 10-15 s, "roughly 1 mph every 1 second". 1 mph/s = 0.447 m/s^2,
# hence RATE_NOM. That is the rate used whenever there is room for it; a large
# drop with little distance left tightens up to RATE_MAX and no further.
#
# RATE_MAX IS NOT A COMFORT NUMBER — it is a structural ceiling. long_mpc.py
# sets CRUISE_MIN_ACCEL = -1.2, so the cruise obstacle physically cannot demand
# a steeper decel than 1.2 m/s^2. Slewing the set speed faster than that would
# simply move a number on the cluster that the car never follows, which is the
# worst of both worlds: it looks like the system reacted and it didn't. A test
# pins RATE_MAX <= abs(CRUISE_MIN_ACCEL) so a future retune cannot break the
# tie silently.
RATE_NOM = 0.45           # m/s^2 ~= 1.0 mph/s — the requested comfortable rate
RATE_MAX = 1.2            # m/s^2 == abs(long_mpc.CRUISE_MIN_ACCEL); see above
RAMP_T_MAX = 15.0         # s — upper bound on how long a ramp is allowed to take
RAMP_D_MAX = 250.0        # m — upper bound on how far ahead a ramp may engage
RAMP_ARRIVE_EARLY_T = 1.0  # s of TRAVEL (scaled by v_ego) to reach the target early
RAMP_UP_DIST = 90.0       # m — window over which the set speed is walked UP into a faster zone

# The upcoming-limit signal comes from OSM through mapd and can flicker for a
# frame when the route match jumps. CONFIRM_N consecutive agreeing frames
# (150 ms at 20 Hz) are required before a ramp engages, so a one-frame ghost
# limit can never move the driver's set speed.
CONFIRM_N = 3

# Monotone-descent latch release. `distance_to_next_limit` legitimately jumps
# UP when the route changes (a turn onto a different road). Small increases are
# GPS noise and must not un-do a descent already committed to; a sustained
# increase of more than this means it is a different zone and the latch is
# dropped.
LATCH_RELEASE_D = 30.0    # m

# Must equal intelligent_cruise_button_management.helpers.get_minimum_set_speed.
# Duplicated rather than imported to keep this module import-light (it is on
# plannerd's path and the tests run without the compiled params extension); a
# test asserts the two agree. NOTE the imperial floor is 20 KPH (~12.4 mph),
# not 20 mph.
MIN_SET_SPEED_KPH_METRIC = 30
MIN_SET_SPEED_KPH_IMPERIAL = 20
V_CRUISE_MAX_KPH = 145    # selfdrive/car/cruise.py


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

    # v3.4.5 predictive set-speed ramp state. v_cruise_target is what the
    # CLUSTER should read right now; cruise_ext follows it and is the only
    # writer of v_cruise_kph while SLA is active.
    self.v_cruise_target = 0.
    self._latch = 0.          # monotone-descent latch (0 = not latched)
    self._d_min = float('inf')  # closest approach seen this descent
    self._confirm_val = 0.    # upcoming-limit value being confirmed
    self._confirm_n = 0       # consecutive frames it has agreed

    # Monotonic deadline after any cruise button activity. This is the ONLY
    # discriminator between "the driver adjusted the set speed" and "the ramp
    # moved it" — see update_state_machine. The v3.4.0 approach compared the
    # cluster value against the value the ramp last commanded, which fails
    # exactly when it matters: a driver press that happens to land on a value
    # the ramp recently passed through would be mistaken for our own command
    # and their carried offset silently discarded.
    self._button_frames = 0

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
      # v3.4.5: ANY cruise button activity — press OR release — arms the
      # driver-intent window. Presses are included deliberately: the set speed
      # moves on the press for a long hold, so a release-only trigger would
      # miss the very case where the cluster is being dragged.
      if b.type in CRUISE_BUTTONS_PLUS or b.type in CRUISE_BUTTONS_MINUS:
        self._button_frames = BUTTON_INTENT_FRAMES
      if b.pressed:
        continue
      if b.type in CRUISE_BUTTONS_PLUS:
        self._hold_deadline["plus"] = now + CONFIRM_HOLD
      elif b.type in CRUISE_BUTTONS_MINUS:
        self._hold_deadline["minus"] = now + CONFIRM_HOLD

  def _button_event_recent(self) -> bool:
    """True while a cruise button event is recent enough to explain a cluster change."""
    return self._button_frames > 0

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
    # Activation ADOPTS the current set speed: the confirming press is
    # swallowed upstream and nothing is written to the cruise speed, so
    # there is no jump. The dynamic ratio is derived from where the set
    # speed already is — 50 set in a 45 zone activates at +11%.
    self._ratio = 0.0
    self._set_ratio_from_cluster()
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
        # v3.4.5: the ratio is re-derived ONLY from a cluster change the DRIVER
        # caused. Inverting the old test is the whole point: previously any
        # unrecognised change re-derived, so the default on ambiguity was to
        # overwrite the driver's carried offset. Now the default is to keep it,
        # and only a recent button event grants permission to change it.
        # Mid-approach the cluster sits BETWEEN zones (the ramp is walking it),
        # so re-deriving there would collapse a carried +20% to whatever value
        # the ramp happened to be passing through.
        if self.v_cruise_cluster_changed and self._button_event_recent():
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
    """Throttle-only clamp while the ramp is holding the set speed down.

    v3.4.5 redefines this off the ramp instead of computing its own coast
    envelope. The old version modelled an assumed 0.35 m/s^2 coast decel and
    compared it against `_next_distance` — a second, independent notion of
    "when should we start slowing", which could and did disagree with the
    ramp's. There is now ONE answer: if the ramp has walked the set speed
    below the current zone's target, the car should not be adding throttle to
    fight it. That also means this can never engage while the ramp is
    inactive, which is what makes it impossible for the gate to brake early
    on its own initiative.

    MUST be called AFTER _update_cruise_ramp() — it reads that frame's target.
    """
    self.gas_gate_active = (self.is_active and self.v_cruise_target > 0.
                            and self.v_cruise_target < self.effective_speed_limit_target - 0.1)

  # ---------- predictive set-speed ramp (v3.4.5) ----------

  def _min_set_speed(self) -> float:
    kph = MIN_SET_SPEED_KPH_METRIC if self.is_metric else MIN_SET_SPEED_KPH_IMPERIAL
    return kph * CV.KPH_TO_MS

  def _update_cruise_ramp(self) -> None:
    """Walk the SET SPEED toward the upcoming zone's target BEFORE the boundary.

    THE MATH. Given the current target v0, the upcoming target v1 < v0 and the
    remaining distance d, a constant-decel walk of the set speed satisfies
    v_set(d) = sqrt(v1^2 + 2*a*d), which reaches exactly v1 at d = 0. Solving
    that envelope for the rate needed to be done by the boundary gives
    a = (v0^2 - v1^2) / (2*d). We choose `a` up front from two independent
    bounds and then hold it fixed for the descent:

        a = clip( max( dv / RAMP_T_MAX,                       # duration bound
                       (v0^2 - v1^2) / (2 * RAMP_D_MAX) ),    # distance bound
                  RATE_NOM, RATE_MAX )

    The duration bound says "never take longer than 15 s". The distance bound
    says "never start further out than 250 m". `max` of the two picks whichever
    is more demanding, and the final clip means the answer is RATE_NOM (1 mph/s,
    what the user asked for) whenever both bounds are slack, and never exceeds
    the MPC's own CRUISE_MIN_ACCEL ceiling.

    Worked example, the design case: 70 -> 45 mph is dv = 11.2 m/s, so the
    duration bound wants 0.745 m/s^2 and the distance bound wants 1.15 m/s^2.
    a = 1.15, and the envelope reaches v0 at d = (v0^2-v1^2)/(2a) = 250 m —
    i.e. it engages at exactly RAMP_D_MAX and takes 11.2/1.15 = 9.7 s. A gentler
    change, 45 -> 35 mph (dv = 4.5), takes the RATE_NOM floor and engages at
    89 m / 10 s. Both land inside the requested 10-15 s feel.

    WHY DISTANCE-PARAMETERISED AND NOT A TIMER: the envelope is re-evaluated
    from the CURRENT d every frame, so it self-corrects. If the car is going
    faster than expected it is deeper into the envelope and the set speed drops
    faster; if mapd revises d, the ramp simply lands on the new answer. There
    is no accumulated ramp state to get out of sync with the road, and no
    division by v_ego, so standstill is safe by construction.

    Up into a faster zone: linear over the last RAMP_UP_DIST metres. This DOES
    raise the set speed slightly before the sign — the explicit intent — and
    the window is kept short so it reads as a blend, not an early overspeed.
    """
    if not self.is_active or self._base_limit <= 0:
      self.v_cruise_target = 0.
      self._latch = 0.
      self._d_min = float('inf')
      self._confirm_n = 0
      return

    current_target = self.effective_speed_limit_target

    # RE-SEED. Three cases where continuity with the previous frame is wrong
    # and the slew cap must be bypassed rather than fought:
    #   * a zone boundary was crossed — current_target just stepped, and the
    #     ramp should be AT the new value, not crawling toward it;
    #   * the driver pressed a cruise button — their intent outranks ours, and
    #     the ratio has just been re-derived from where they put it;
    #   * first active frame — there is no previous value to be continuous with.
    # Seeding also clears the descent latch: a driver press mid-descent must be
    # able to raise the set speed again.
    if (self.speed_limit_final_last_changed or self._button_event_recent()
        or self.v_cruise_target <= 0.):
      self.v_cruise_target = self._clamp_set_speed(current_target)
      self._latch = 0.
      self._d_min = float('inf')
      self._confirm_n = 0
      return

    # CONFIRMATION. Require CONFIRM_N agreeing frames before acting on an
    # upcoming limit, so a single-frame OSM ghost can't move the set speed.
    next_final = self._next_limit_final
    if next_final > 0. and abs(next_final - self._confirm_val) < 0.1:
      self._confirm_n += 1
    else:
      self._confirm_val = next_final
      self._confirm_n = 1

    next_target = self.next_zone_target
    d = self._next_distance
    engaged = self._confirm_n >= CONFIRM_N and next_target > 0. and d > 0.

    target = current_target
    if engaged and next_target < current_target:
      dv = current_target - next_target
      a = max(dv / RAMP_T_MAX,
              (current_target ** 2 - next_target ** 2) / (2. * RAMP_D_MAX))
      a = min(max(a, RATE_NOM), RATE_MAX)
      # Arrive early by a fixed TRAVEL TIME, i.e. scaled by v_ego. The v3.4.0
      # version scaled it by next_target, which made the early-arrival margin
      # depend on the destination speed rather than on how fast the boundary is
      # actually approaching — backwards, and it vanished at low speed limits.
      d_eff = max(0., d - self.v_ego * RAMP_ARRIVE_EARLY_T)
      target = min(current_target, (next_target ** 2 + 2. * a * d_eff) ** 0.5)

      # MONOTONE-DESCENT LATCH. d is a great-circle distance from OSM and can
      # tick back up on noise; without this the set speed would visibly bounce
      # back up mid-approach. Released only on a SUSTAINED increase, which
      # means the route changed and this is a different zone.
      self._d_min = min(self._d_min, d)
      if d > self._d_min + LATCH_RELEASE_D:
        self._latch = 0.
        self._d_min = d
      if self._latch > 0.:
        target = min(target, self._latch)
      self._latch = target
    elif engaged and next_target > current_target:
      self._latch = 0.
      self._d_min = float('inf')
      blend = max(0., min(1., 1. - d / RAMP_UP_DIST))
      target = current_target + (next_target - current_target) * blend
    else:
      self._latch = 0.
      self._d_min = float('inf')

    # Slew cap. Because the re-seed above guarantees v_cruise_target is never 0
    # while active, this is live on the very first engaged frame — the case the
    # v3.4.0 `if self.v_cruise_target > 0.` guard silently skipped.
    max_step = RATE_MAX * DT_MDL
    target = min(max(target, self.v_cruise_target - max_step), self.v_cruise_target + max_step)

    self.v_cruise_target = self._clamp_set_speed(target)

  def _clamp_set_speed(self, v: float) -> float:
    return min(max(v, self._min_set_speed()), V_CRUISE_MAX_KPH * CV.KPH_TO_MS)

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

    # ORDER IS LOAD-BEARING: the gas gate is defined off this frame's ramp
    # target (see _update_gas_gate), so the ramp must run first. v3.4.0 had
    # these the other way round and the gate read the previous frame's value.
    self._update_cruise_ramp()
    self._update_gas_gate()
    self.update_events(events_sp)

    # Aged out LAST: everything above this line in this frame must see the same
    # answer from _button_event_recent(), or the state machine and the ramp
    # could disagree about whether the driver just intervened.
    self._button_frames = max(0, self._button_frames - 1)

    self.long_enabled_prev = self.long_enabled
    self._had_speed_limit = self._has_speed_limit
    self.prev_v_cruise_cluster_conv = self.v_cruise_cluster_conv
    self.prev_speed_limit_final_last_conv = self.speed_limit_final_last_conv

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
