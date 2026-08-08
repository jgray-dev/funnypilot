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
  * If the set speed already equals the limit DURING THE WINDOW, SLA activates
    immediately (nothing to confirm, ratio 0).
  * v3.6.3 — OUTSIDE THE WINDOW, THE CRUISE BUTTONS DO NOTHING TO SLA. Once
    the 6 s lapses the state returns to inactive, the UI takes the arrow down,
    and a set-speed change is only a set-speed change. To ask again, cycle
    longitudinal control off and on: that routes through `disabled` and opens
    a fresh window. The window is the permission, and it expires.

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
#
# FunnyPilot v3.4.7 — LONGER RUNWAY. With the v3.4.6 clock fix the ramp finally
# runs on a moving car, and on the road it lands late: the set speed reaches the
# upcoming target AT the boundary, which is not the same as the CAR being at the
# new speed when it crosses. The set speed is a request, and the car trails it —
# the cruise obstacle has to bleed the remaining error off after the number has
# already stopped moving. So the runway is extended at both ends:
#
#   * RAMP_ARRIVE_EARLY_T 1 -> 3 s. This is the one that fixes the reported
#     symptom. It is TRAVEL time, so at 70 mph the set speed is done ~94 m
#     before the sign, leaving the car room to actually settle onto it rather
#     than still be decelerating through the boundary.
#   * RAMP_D_MAX 250 -> 400 m and RAMP_T_MAX 15 -> 20 s. The early-arrival
#     margin is subtracted from the distance the envelope gets to work with, so
#     without widening these bounds it would just buy itself a steeper `a` and
#     the descent would get harsher instead of earlier. 70 -> 45 mph now engages
#     ~494 m out over ~15.6 s (was ~281 m / ~9.7 s).
#
# RATE_NOM is deliberately NOT touched: 1 mph/s is what was asked for, and it
# still governs every gentle change, which now simply starts sooner.
RATE_NOM = 0.45           # m/s^2 ~= 1.0 mph/s — the requested comfortable rate
RATE_MAX = 1.2            # m/s^2 == abs(long_mpc.CRUISE_MIN_ACCEL); see above
RAMP_T_MAX = 20.0         # s — upper bound on how long a ramp is allowed to take
RAMP_D_MAX = 400.0        # m — upper bound on how far ahead a ramp may engage
RAMP_ARRIVE_EARLY_T = 3.0  # s of TRAVEL (scaled by v_ego) to reach the target early

# FunnyPilot v3.4.8 — THE UP-RAMP WAS DIMENSIONALLY INCAPABLE OF FINISHING.
# It walked the set speed up over a FIXED 90 m while the output is bounded by a
# RATE (RATE_MAX * DT_MDL). Those are different units, so the window silently
# truncates whenever dv > RATE_MAX * (d / v_ego): at 55 mph, 90 m is 3.66 s of
# travel, and 3.66 * 1.2 = 4.4 m/s = 9.8 mph of a 15 mph rise. The rest arrived
# as the boundary re-seed — the reported "we entered the new zone below target,
# then it snapped up".
#
# The window is now a TRAVEL TIME, so it holds its meaning at any speed, and it
# is sized from the rise itself: t = dv / RATE_NOM, i.e. exactly long enough to
# complete at the nominal rate. RAMP_UP_T_MAX bounds how early the set speed may
# go over the current limit, which is the reason this side stays much shorter
# than the descent: walking DOWN early is free, walking UP early is speeding
# early. When dv is too large to fit in RAMP_UP_T_MAX the ramp still truncates —
# deliberately. Finishing the rise is not worth going 15 mph over the posted
# limit 300 m before the sign; the boundary re-seed picks up the remainder.
RAMP_UP_T = 5.0           # s of travel over which a rise is walked, at RATE_NOM
RAMP_UP_T_MAX = 8.0       # s — ceiling on how early the set speed may exceed the current limit
RAMP_UP_D_MIN = 40.0      # m — floor so the window survives low speed / standstill

# The upcoming-limit signal comes from OSM through mapd and can flicker for a
# frame when the route match jumps. CONFIRM_N consecutive agreeing frames
# (150 ms at 20 Hz) are required before a ramp engages, so a one-frame ghost
# limit can never move the driver's set speed.
CONFIRM_N = 3

# FunnyPilot v3.4.8 — CONFIRM_N GUARDS ENTRY BUT NOTHING GUARDED CONTINUATION.
# `engaged` was a cliff: one dropped mapd frame reset _confirm_n to 1, so for at
# least CONFIRM_N frames the ramp fell through to `target = current_target` and
# the set speed visibly walked the WRONG WAY before resuming — the reported
# "flickered down a mph, continued going up". The 1 Hz liveMapDataSP publisher,
# a route re-match, and `d` reaching 0 before the current limit flips all
# produce that blink routinely.
#
# So an engagement, once confirmed, is carried through a dropout by DEAD
# RECKONING the remembered zone: d keeps closing at v_ego, which is what the car
# is actually doing. Bounded at 1 s — long enough to bridge any blink, short
# enough that a genuinely vanished zone cannot hold the set speed hostage.
ENGAGE_GRACE_FRAMES = int(1.0 / DT_MDL)  # 20 frames = 1 s

# FunnyPilot v3.4.8 — gas gate guards (see _update_gas_gate for the post-mortem).
# GATE_V_MARGIN keeps the gate from chattering on either side of the target; the
# clip rate limiter in the planner (0.05 m/s^2 per frame) smooths what is left.
# GATE_MAX_FRAMES is a WATCHDOG, not a tuning knob: the longest legitimate hold
# is one descent, ~15.6 s at the widest v3.4.7 geometry, so anything past 30 s
# is a latch that should not exist and the throttle goes back to the driver.
GATE_V_MARGIN = 0.5       # m/s the car must exceed the ramp target before gating
GATE_MAX_FRAMES = int(30.0 / DT_MDL)

# FunnyPilot v3.4.9 — how far the ramp target must sit from this zone's own
# target before the cluster is considered DISPLACED, i.e. before a driver's
# cruise press has to be read as a delta on the ramp rather than as an absolute
# statement about the current zone. One display unit is 0.447 m/s (1 mph) /
# 0.278 m/s (1 kph); 0.2 m/s is comfortably under both, so a set speed parked on
# the zone target still takes the plain absolute derivation.
RAMP_DISPLACED_TH = 0.2   # m/s

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

    # v3.4.8 engagement hysteresis: a confirmed upcoming zone is carried through
    # a signal dropout by dead reckoning rather than collapsing the ramp.
    self._engage_grace = 0
    self._engage_target = 0.
    self._engage_d = 0.
    # v3.4.8 gas gate watchdog (see _update_gas_gate)
    self._gate_frames = 0

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

    # v3.4.9: pending driver set-speed delta, in m/s, measured against the
    # value the RAMP had the cluster at. See _apply_driver_set_speed_change.
    self._driver_delta = 0.
    self._driver_adjust = False
    self._driver_displaced = False

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

  @property
  def _ramp_displaced(self) -> bool:
    """True while the ramp is holding the cluster somewhere other than this
    zone's own target — i.e. the cluster value is NOT a statement of driver
    intent about the current zone."""
    if self.v_cruise_target <= 0. or self._base_limit <= 0.:
      return False
    return abs(self.v_cruise_target - self._clamp_set_speed(self.effective_speed_limit_target)) > RAMP_DISPLACED_TH

  @property
  def busy(self) -> bool:
    """FunnyPilot v3.5.0 — True while SLA is the reason the car's speed is
    changing (mid-ramp or gas-gating). Read-only; SCC-Learn uses it to refuse
    to learn a speed dip that a zone boundary explains, which would otherwise
    put a permanent corner cap at every speed-limit sign on the commute."""
    return bool(self.is_active and (self.gas_gate_active or self._ramp_displaced))

  def _apply_driver_set_speed_change(self) -> None:
    """FunnyPilot v3.4.9 — a cruise adjustment DURING a ramp is a DELTA, not an
    absolute statement of the driver's offset for this zone.

    THE BUG THIS FIXES. `_set_ratio_from_cluster` reads the whole cluster value
    as "what the driver wants relative to the current limit". That is true when
    the cluster is parked on limit*(1+ratio) and FALSE for the entire duration
    of a ramp, because the ramp is what put the cluster where it is. Concretely:
    +10% carried into a 45 mph zone (target 49.5), descending toward a 25 zone,
    cluster currently walked down to 35. One tap of `+` and the old code
    re-derived ratio = (36 - 45)/45 = -20%. The carried offset is destroyed and
    the new zone is then entered at 25 * 0.8 = 20 mph — the reported "it forgets
    where SLA was set before, and it's all buggy when we get to the new zone".

    So: while the ramp has the cluster displaced, the driver's press moves the
    ratio by exactly what they added ON TOP of the ramp's value; parked on the
    zone target (no ramp), the old absolute derivation is still correct and is
    what runs. Either way `_driver_delta` records the move so the ramp can adopt
    the driver's value without restarting its descent.
    """
    self._driver_adjust = True
    self._driver_displaced = False
    if self._base_limit <= 0. or self.v_cruise_cluster <= 0.:
      self._driver_delta = 0.
      return

    if self._ramp_displaced:
      self._driver_displaced = True
      self._driver_delta = self.v_cruise_cluster - self.v_cruise_target
      ratio = self._ratio + self._driver_delta / self._base_limit
      self._ratio = max(-RATIO_LIMIT, min(RATIO_LIMIT, ratio))
    else:
      self._driver_delta = 0.
      self._set_ratio_from_cluster()

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
        # v3.4.9: mid-ramp the cluster sits where the RAMP put it, so an
        # absolute re-derivation reads our own displacement as the driver's
        # offset. _apply_driver_set_speed_change handles both cases.
        if self.v_cruise_cluster_changed and self._button_event_recent():
          self._apply_driver_set_speed_change()
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
        # FunnyPilot v3.6.3 — THE WINDOW IS THE WHOLE PERMISSION, AND OUTSIDE
        # IT A SET-SPEED CHANGE IS JUST A SET-SPEED CHANGE.
        #
        # Owner-reported and explicit: once the 6 s window lapses and the UI
        # takes the arrow down, adjusting the cruise speed must not touch SLA
        # at all. The only way back is to cycle longitudinal control off and on,
        # which routes through `disabled` and re-enters preActive below with a
        # fresh 6 s — a deliberate gesture the driver chooses, rather than a
        # side effect of moving the set speed.
        #
        # TWO DOORS WERE REMOVED HERE, and both were reachable by moving the
        # set speed while no arrow was showing:
        #
        #   1. `set_speed_matches_limit -> _activate()`. Dialling the cluster
        #      onto the sign activated outright, with no prompt at any point.
        #   2. v3.5.5's `_button_event_recent() -> _enter_pre_active()`. That
        #      was added to escape a reported lockout ("changing speed to
        #      enable it does nothing"), but it made the expiry meaningless:
        #      the press RE-OPENED the window and, because press and release
        #      are separate events 100-300 ms apart while the state machine
        #      runs every 50 ms, the release of that SAME click then landed in
        #      the freshly opened window and confirmed it. One click, arrow
        #      visible for a tenth of a second, SLA on.
        #
        # THE LOCKOUT v3.5.5 WORRIED ABOUT IS NOW THE INTENDED BEHAVIOUR, so
        # it is not a regression — but it IS the same behaviour that was once
        # reported as a bug, and anyone reading that history needs to know it
        # was reversed on purpose. The long-control cycle is the documented
        # reset; see the `disabled` branch below.
        if self._has_speed_limit and (self.speed_limit_final_last_changed or not self._had_speed_limit):
          # new zone (or first limit seen): offer activation
          self._enter_pre_active()
        else:
          # Nothing else may leave `inactive`. Releases are dropped so a press
          # made here cannot be consumed as a confirm by a window opened later.
          self._clear_releases()

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

    v3.4.8 — THIS IS THE BUG THAT STRANDED THE CAR. The test above is pure
    set-speed geometry: it never asked how fast the car was actually going. But
    the gate's whole justification is "do not add throttle to FIGHT the ramp",
    and there is no fight when v_ego is already at or below the ramp's own
    target. Entering a zone below target (which the truncated up-ramp made
    routine) with any lower zone inside the v3.4.7 ~490 m envelope therefore
    produced: ramp engaged -> gate on -> accel_clip[1] pinned to coast accel ->
    a car that will not accelerate, cannot be persuaded by the pedal (the gate
    is still there when the override releases), and only recovers when SLA is
    cycled. Reported as ~10 s of coasting on the highway; nothing in the old
    condition bounded it, which is why "it could have slowed to a stop" was a
    fair reading.

    Three narrowings, all in the fail-safe direction (the gate can now only
    engage in strictly fewer situations than before):

      * v_ego must actually be ABOVE the target. This is the fix.
      * compare against the CLAMPED target. `v_cruise_target` is passed through
        _clamp_set_speed and `effective_speed_limit_target` was not, so a target
        above V_CRUISE_MAX_KPH (a high limit with a carried positive ratio) or
        below the min set speed made `clamped < unclamped` true FOREVER — a
        latched gate with no exit at all.
      * a duration backstop. Nothing here should ever hold throttle off for
        GATE_MAX_S; if something does, it is a bug, and the car coasting on a
        highway is not an acceptable way to find out about it.
    """
    if not self.is_active or self.v_cruise_target <= 0.:
      self.gas_gate_active = False
      self._gate_frames = 0
      return

    ceiling = self._clamp_set_speed(self.effective_speed_limit_target)
    holding_down = self.v_cruise_target < ceiling - 0.1
    above_target = self.v_ego > self.v_cruise_target + GATE_V_MARGIN

    if not (holding_down and above_target):
      self.gas_gate_active = False
      self._gate_frames = 0
      return

    self._gate_frames += 1
    self.gas_gate_active = self._gate_frames <= GATE_MAX_FRAMES

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

    The duration bound says "never take longer than RAMP_T_MAX". The distance
    bound says "never start further out than RAMP_D_MAX". `max` of the two picks
    whichever is more demanding, and the final clip means the answer is RATE_NOM
    (1 mph/s, what the user asked for) whenever both bounds are slack, and never
    exceeds the MPC's own CRUISE_MIN_ACCEL ceiling.

    Worked example, the design case: 70 -> 45 mph is dv = 11.2 m/s, so the
    duration bound wants 0.56 m/s^2 and the distance bound wants 0.72 m/s^2.
    a = 0.72, and the envelope reaches v0 at d_eff = (v0^2-v1^2)/(2a) = 400 m —
    i.e. it engages at exactly RAMP_D_MAX of USABLE distance and takes
    11.2/0.72 = 15.6 s. Note that d_eff is what is left after the early-arrival
    margin, so the ramp actually engages RAMP_ARRIVE_EARLY_T * v_ego further out
    than that (~494 m here) and is DONE ~94 m before the sign. A gentler change,
    45 -> 35 mph (dv = 4.5), still takes the RATE_NOM floor: 179 m of envelope
    over 10 s, engaging ~239 m out.

    WHY DISTANCE-PARAMETERISED AND NOT A TIMER: the envelope is re-evaluated
    from the CURRENT d every frame, so it self-corrects. If the car is going
    faster than expected it is deeper into the envelope and the set speed drops
    faster; if mapd revises d, the ramp simply lands on the new answer. There
    is no accumulated ramp state to get out of sync with the road, and no
    division by v_ego, so standstill is safe by construction.

    Up into a faster zone: linear over a window sized as a TRAVEL TIME (v3.4.8;
    it used to be a fixed 90 m, which the rate limiter truncated at any real
    speed — see RAMP_UP_T). This DOES raise the set speed slightly before the
    sign, the explicit intent, and RAMP_UP_T_MAX keeps that lead short because
    early on this side means over the posted limit.
    """
    if not self.is_active or self._base_limit <= 0:
      self.v_cruise_target = 0.
      self._latch = 0.
      self._d_min = float('inf')
      self._confirm_n = 0
      self._engage_grace = 0
      self._driver_delta = 0.
      self._driver_adjust = False
      self._driver_displaced = False
      return

    current_target = self.effective_speed_limit_target

    # RE-SEED. Two cases where continuity with the previous frame is wrong and
    # the slew cap must be bypassed rather than fought:
    #   * a zone boundary was crossed — current_target just stepped, and the
    #     ramp should be AT the new value, not crawling toward it;
    #   * first active frame — there is no previous value to be continuous with.
    if self.speed_limit_final_last_changed or self.v_cruise_target <= 0.:
      self.v_cruise_target = self._clamp_set_speed(current_target)
      self._latch = 0.
      self._d_min = float('inf')
      self._confirm_n = 0
      self._engage_grace = 0
      self._driver_delta = 0.
      self._driver_adjust = False
      self._driver_displaced = False
      return

    # DRIVER ADJUSTMENT (v3.4.9). The v3.4.5 form treated a button press like a
    # boundary crossing: seed to `current_target` and clear the descent state.
    # Both halves of that are wrong mid-ramp. Seeding to `current_target` throws
    # the set speed back UP to the full zone target the driver was already
    # descending away from, and clearing `_latch` / `_d_min` / `_confirm_n`
    # restarts the descent from scratch — so a driver who taps twice on the way
    # into a zone gets the ramp aborted and re-derived twice, which is the other
    # half of the reported buggy behaviour.
    #
    # Instead: ADOPT the value the driver just set (the cluster IS their intent,
    # whatever the ramp had been doing), shift the monotone-descent latch by the
    # same delta so it does not immediately claw the adjustment back, and keep
    # every other piece of ramp state. The ramp is then HELD for the rest of the
    # intent window so their press can settle through card -> carState without
    # us writing over it.
    if self._button_event_recent():
      if self._driver_adjust:
        if self._driver_displaced:
          # RAMP DISPLACED: adopt the value the driver just set and carry the
          # descent, shifting the latch by the same amount so the monotone
          # guard does not immediately claw their adjustment back.
          self.v_cruise_target = self._clamp_set_speed(self.v_cruise_target + self._driver_delta)
          if self._latch > 0.:
            self._latch = self._clamp_set_speed(self._latch + self._driver_delta)
        else:
          # NOT displaced: the cluster was a plain statement about this zone and
          # the ratio has just been re-derived (and RATIO_LIMIT-clamped) from
          # it, so seed to the clamped target — that is what enforces the rail.
          self.v_cruise_target = self._clamp_set_speed(current_target)
          self._latch = 0.
          self._d_min = float('inf')
        self._driver_adjust = False
        self._driver_displaced = False
        self._driver_delta = 0.
      return

    # CONFIRMATION. Require CONFIRM_N agreeing frames before acting on an
    # upcoming limit, so a single-frame OSM ghost can't move the set speed.
    next_final = self._next_limit_final
    if next_final > 0. and abs(next_final - self._confirm_val) < 0.1:
      self._confirm_n += 1
    else:
      self._confirm_val = next_final
      self._confirm_n = 1

    # ENGAGEMENT, with continuation hysteresis (v3.4.8). Confirming is the hard
    # part; once confirmed, a blink in the signal must not collapse the ramp and
    # walk the set speed backwards. Dead-reckon the remembered zone instead: the
    # car really is still closing on it at v_ego. See ENGAGE_GRACE_FRAMES.
    next_target = self.next_zone_target
    d = self._next_distance
    if self._confirm_n >= CONFIRM_N and next_target > 0. and d > 0.:
      self._engage_grace = ENGAGE_GRACE_FRAMES
      self._engage_target = next_target
      self._engage_d = d
    elif self._engage_grace > 0:
      self._engage_grace -= 1
      self._engage_d = max(0.1, self._engage_d - self.v_ego * DT_MDL)
      next_target = self._engage_target
      d = self._engage_d
    else:
      next_target = 0.
      d = 0.

    engaged = next_target > 0. and d > 0.

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
      # v3.4.8: window is a travel time sized from the rise, not a fixed 90 m.
      # t = dv / RATE_NOM completes at the nominal rate; RAMP_UP_T_MAX bounds
      # how early we may sit above the current limit. RAMP_UP_D_MIN keeps the
      # window finite at low v_ego (and non-zero at standstill, where the old
      # form was fine only because it never divided by v_ego).
      dv = next_target - current_target
      t_up = min(RAMP_UP_T_MAX, max(RAMP_UP_T, dv / RATE_NOM))
      d_up = max(RAMP_UP_D_MIN, self.v_ego * t_up)
      blend = max(0., min(1., 1. - d / d_up))
      target = current_target + dv * blend
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
    """FunnyPilot v3.4.9 — THIS IS THE RAMP TARGET, NOT THE ZONE TARGET.

    THE BUG. `v_sla` goes into the speed governor's min() alongside the cluster
    set speed. Returning `effective_speed_limit_target` — the CURRENT zone's
    value — meant SLA itself pinned the car to the old zone for the whole
    approach to a FASTER one. The ramp did its job (the cluster visibly walked
    up, exactly as designed) and then this line threw the result away: the
    governor picked min(rising cluster, old zone target) = old zone target, so
    the long control did not react at all until the boundary crossing re-seeded
    `effective_speed_limit_target` and the whole rise arrived as one step. That
    is precisely the reported "I see the set speed going up but it stays at the
    previous speed, then jumps fully when we enter the zone".

    The DOWN ramp never showed it because there the cluster is the more
    restrictive of the two, so min() picked the ramp's value by accident.

    `v_cruise_target` IS the speed SLA is asking for right now, in both
    directions, and it is already clamped by `_clamp_set_speed`. Publishing it
    makes SLA's own governor entry agree with the number on the cluster instead
    of fighting it, and leaves the descent behaviour bit-identical (there the
    two values are the same thing).
    """
    if self.is_active and self._base_limit > 0:
      if self.v_cruise_target > 0.:
        return self.v_cruise_target
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
