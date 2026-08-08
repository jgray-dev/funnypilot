"""
FunnyPilot v3.3.3 — Speed Limit Assist tests (preActive arrow activation +
ratio carryover + pre-zone gas gate).

Import-light: uses a stub Params object, so it runs without the compiled
openpilot environment:
  python3 -m pytest sunnypilot/selfdrive/controls/lib/speed_limit/tests/test_speed_limit_assist.py
"""
import time

from cereal import car, custom
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Mode
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import (
  SpeedLimitAssist, ACTIVE_STATES, DISABLED_GUARD_PERIOD, PRE_ACTIVE_WINDOW, RATIO_LIMIT,
  BUTTON_INTENT_FRAMES, CONFIRM_N, V_CRUISE_UNSET,
)

ButtonType = car.CarState.ButtonEvent.Type
EventNameSP = custom.OnroadEventSP.EventName
SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState

MPH = CV.MPH_TO_MS
GUARD_FRAMES = int(DISABLED_GUARD_PERIOD / DT_MDL) + 2
WINDOW_FRAMES = int(PRE_ACTIVE_WINDOW / DT_MDL)


class FakeParams:
  def __init__(self, mode=Mode.assist, metric=False):
    self.mode = mode
    self.metric = metric

  def get(self, key, return_default=False):
    if key == "SpeedLimitMode":
      return int(self.mode)
    return None

  def get_bool(self, key):
    if key == "IsMetric":
      return self.metric
    return False


class FakeEvents:
  def __init__(self):
    self.names = []

  def add(self, name):
    self.names.append(name)

  def clear(self):
    self.names = []


def make_sla(mode=Mode.assist):
  CP = car.CarParams.new_message(openpilotLongitudinalControl=True, pcmCruise=False)
  CP_SP = custom.CarParamsSP.new_message()
  return SpeedLimitAssist(CP, CP_SP, params=FakeParams(mode=mode))


def make_button_cs(button_type, pressed):
  CS = car.CarState.new_message()
  be = CS.init('buttonEvents', 1)
  be[0].type = button_type
  be[0].pressed = pressed
  return CS


def release(sla, button_type):
  sla.update_car_state(make_button_cs(button_type, False))


def step(sla, events, cluster_mph=50., limit_mph=45., long_enabled=True, has_limit=True, v_ego_mph=None,
         next_limit_mph=0., next_dist=0., n=1):
  if v_ego_mph is None:
    v_ego_mph = cluster_mph
  for _ in range(n):
    sla.update(long_enabled, False, v_ego_mph * MPH, 0.0, cluster_mph * MPH,
               limit_mph * MPH if has_limit else 0.0, limit_mph * MPH, has_limit, 0.0, events,
               next_speed_limit_final=next_limit_mph * MPH, next_distance=next_dist)


def engage(sla, events, **kw):
  """Run the disabled guard; with a limit present this lands in preActive."""
  step(sla, events, n=GUARD_FRAMES, **kw)


def activate(sla, events, cluster_mph, limit_mph):
  """Engage + confirm in the arrow's direction; the set speed is ADOPTED unchanged."""
  engage(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph)
  if not sla.is_active:  # set == limit auto-activates during engage
    assert sla.state == SpeedLimitAssistState.preActive
    release(sla, ButtonType.accelCruise if cluster_mph < limit_mph else ButtonType.decelCruise)
    step(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph)
  assert sla.is_active
  events.clear()


class TestArming:
  def test_initial_state(self):
    sla = make_sla()
    assert sla.state == SpeedLimitAssistState.disabled
    assert sla.output_v_target == V_CRUISE_UNSET

  def test_mode_off_stays_disabled(self):
    sla = make_sla(mode=Mode.warning)
    events = FakeEvents()
    step(sla, events, n=GUARD_FRAMES)
    assert sla.state == SpeedLimitAssistState.disabled

  def test_engage_with_limit_enters_pre_active(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.state == SpeedLimitAssistState.preActive
    assert EventNameSP.speedLimitPreActive in events.names

  def test_engage_without_limit_stays_inactive(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, has_limit=False, limit_mph=0.)
    assert sla.state == SpeedLimitAssistState.inactive

  def test_limit_appearing_while_inactive_prompts(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, has_limit=False, limit_mph=0.)
    step(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.state == SpeedLimitAssistState.preActive


class TestTheWindowIsTheWholePermission:
  """FunnyPilot v3.6.3 — once the 6 s lapses, the cruise buttons do NOT touch SLA.

  Owner-reported and explicit: after the UI takes the arrow down, changing the
  set speed must only change the set speed. The way to ask again is to cycle
  longitudinal control, which opens a fresh window.

  THIS REVERSES v3.5.5 ON PURPOSE. That release added a cruise press as an
  escape from `inactive`, because a lockout had been reported. The escape made
  the expiry meaningless in a way that was worse: press and release are
  SEPARATE button events 100-300 ms apart while this state machine runs every
  50 ms, so the press re-opened the window and the release of that same click
  landed inside it and confirmed. One click, arrow visible for a tenth of a
  second, SLA on. The lockout is now the intended behaviour and the long-control
  cycle is the documented reset.
  """

  def _timed_out(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.state == SpeedLimitAssistState.preActive
    # let the window expire with the SAME zone still in force
    step(sla, events, cluster_mph=40., limit_mph=45., n=WINDOW_FRAMES + 2)
    assert sla.state == SpeedLimitAssistState.inactive
    return sla, events

  def test_it_really_does_time_out_first(self):
    """Anti-vacuous: if this stopped landing in `inactive` every test below
    would pass without exercising anything."""
    sla, _ = self._timed_out()
    assert not sla.is_active

  def test_a_cruise_press_does_not_reopen_the_window(self):
    """MUTATION: restore the v3.5.5 `_button_event_recent()` branch."""
    sla, events = self._timed_out()
    release(sla, ButtonType.decelCruise)
    step(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.state == SpeedLimitAssistState.inactive
    assert not sla.is_active

  def test_a_single_click_cannot_activate(self):
    """THE REPORTED BEHAVIOUR, PINNED. The press re-armed and its own release
    confirmed, so one click turned SLA on with no visible prompt."""
    sla, events = self._timed_out()
    release(sla, ButtonType.accelCruise)          # 40 in a 45 zone: the old arrow said UP
    step(sla, events, cluster_mph=40., limit_mph=45., n=4)
    assert not sla.is_active

  def test_repeated_presses_still_cannot_activate(self):
    """Not merely delayed by one press — the door is shut. MUTATION: keep any
    inactive -> preActive transition driven by button events."""
    sla, events = self._timed_out()
    for _ in range(6):
      release(sla, ButtonType.accelCruise)
      step(sla, events, cluster_mph=40., limit_mph=45., n=3)
    assert sla.state == SpeedLimitAssistState.inactive
    assert not sla.is_active

  def test_dialing_onto_the_limit_no_longer_activates(self):
    """The other door out of `inactive`, and the quieter one: walking the
    cluster onto the sign used to activate outright with no prompt at any
    point. That is still 'changing my set speed', so it must do nothing."""
    sla, events = self._timed_out()
    step(sla, events, cluster_mph=45., limit_mph=45., n=4)
    assert not sla.is_active
    assert sla.state == SpeedLimitAssistState.inactive

  def test_cycling_longitudinal_control_reopens_it(self):
    """THE DOCUMENTED RESET, and the reason the above is not a lockout.
    Long control off routes SLA through `disabled`; back on gives a fresh
    window, and a directional press inside it activates as always."""
    sla, events = self._timed_out()
    step(sla, events, cluster_mph=40., limit_mph=45., long_enabled=False, n=3)
    assert sla.state == SpeedLimitAssistState.disabled
    step(sla, events, cluster_mph=40., limit_mph=45., n=GUARD_FRAMES + 3)
    assert sla.state == SpeedLimitAssistState.preActive
    release(sla, ButtonType.accelCruise)
    step(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.is_active

  def test_a_new_zone_still_offers_activation(self):
    """The other legitimate door stays open: a zone change is an event the
    road produced, not a set-speed adjustment."""
    sla, events = self._timed_out()
    step(sla, events, cluster_mph=40., limit_mph=55.)
    assert sla.state == SpeedLimitAssistState.preActive

  def test_no_limit_means_no_prompt(self):
    """A press with nothing to assist toward must stay quiet."""
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, has_limit=False, limit_mph=0.)
    assert sla.state == SpeedLimitAssistState.inactive
    release(sla, ButtonType.decelCruise)
    step(sla, events, has_limit=False, limit_mph=0.)
    assert sla.state == SpeedLimitAssistState.inactive


class TestPreActiveConfirm:
  def test_up_confirm_activates_and_adopts_set_speed(self):
    # 40 set in a 45 zone: up arrow; pressing UP within the window activates.
    # The set speed is ADOPTED unchanged: target stays 40, ratio -11%.
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    release(sla, ButtonType.accelCruise)
    step(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.is_active
    assert abs(sla.dynamic_offset_ratio - (40. - 45.) / 45.) < 1e-6
    assert abs(sla.output_v_target - 40. * MPH) < 1e-6  # no jump on activation
    assert EventNameSP.speedLimitActive in events.names

  def test_down_confirm_activates_and_adopts_set_speed(self):
    # 45 set in a 40 zone: down arrow; pressing DOWN activates at +12.5%,
    # still doing 45 — no jerk
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=45., limit_mph=40.)
    release(sla, ButtonType.decelCruise)
    step(sla, events, cluster_mph=45., limit_mph=40.)
    assert sla.is_active
    assert abs(sla.dynamic_offset_ratio - (45. - 40.) / 40.) < 1e-6
    assert abs(sla.output_v_target - 45. * MPH) < 1e-6  # no jump on activation

  def test_adopted_ratio_carries_to_next_zone(self):
    # activate at 50 in a 45 (+11%) via the arrow; a 35 zone -> 35 * 1.111 = 38.9
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=50., limit_mph=45.)
    release(sla, ButtonType.decelCruise)
    step(sla, events, cluster_mph=50., limit_mph=45.)
    assert sla.is_active
    ratio = (50. - 45.) / 45.
    step(sla, events, cluster_mph=50., limit_mph=35.)
    assert abs(sla.output_v_target - 35. * (1. + ratio) * MPH) < 1e-6

  def test_wrong_direction_does_not_activate(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)  # up arrow
    release(sla, ButtonType.decelCruise)                 # user presses down
    step(sla, events, cluster_mph=39., limit_mph=45.)
    assert not sla.is_active
    assert sla.state == SpeedLimitAssistState.preActive

  def test_stale_release_expires(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    release(sla, ButtonType.accelCruise)
    sla._hold_deadline["plus"] = time.monotonic() - 0.01  # expired
    step(sla, events, cluster_mph=40., limit_mph=45.)
    assert not sla.is_active

  def test_window_times_out(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    step(sla, events, cluster_mph=40., limit_mph=45., n=WINDOW_FRAMES + 2)
    assert sla.state == SpeedLimitAssistState.inactive
    # a press after the timeout is a plain speed adjustment
    release(sla, ButtonType.accelCruise)
    step(sla, events, cluster_mph=41., limit_mph=45.)
    assert not sla.is_active

  def test_zone_change_reprompts_after_timeout(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    step(sla, events, cluster_mph=40., limit_mph=45., n=WINDOW_FRAMES + 2)
    assert sla.state == SpeedLimitAssistState.inactive
    step(sla, events, cluster_mph=40., limit_mph=35.)  # new zone
    assert sla.state == SpeedLimitAssistState.preActive

  def test_matching_set_speed_activates_without_press(self):
    # set speed already equals the limit: nothing to confirm
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=45., limit_mph=45.)
    step(sla, events, cluster_mph=45., limit_mph=45.)
    assert sla.is_active
    assert sla.dynamic_offset_ratio == 0.0

  def test_dialing_onto_limit_activates_inside_the_window(self):
    """Still true DURING the prompt: with the set speed on the sign there is
    no direction left to press, so the match is the confirmation.

    v3.6.3 narrowed this to the window only — see
    TestTheWindowIsTheWholePermission for the outside-the-window case, which
    is now inert."""
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.state == SpeedLimitAssistState.preActive
    step(sla, events, cluster_mph=45., limit_mph=45.)
    assert sla.is_active


class TestRatioCarryover:
  def _active_with_ratio(self, sla, events, set_mph, limit_mph):
    activate(sla, events, cluster_mph=limit_mph, limit_mph=limit_mph)
    # v3.4.5: re-derivation is now gated on a RECENT BUTTON EVENT, so a manual
    # adjustment must be modelled as one. Without this the cluster change is
    # indistinguishable from the ramp moving the set speed itself, and SLA
    # (correctly) refuses to re-read its offset from its own output.
    release(sla, ButtonType.accelCruise if set_mph > limit_mph else ButtonType.decelCruise)
    step(sla, events, cluster_mph=set_mph, limit_mph=limit_mph)  # manual adjust
    assert sla.is_active
    events.clear()

  def test_users_example_60_in_50_then_30_zone(self):
    # active in a 50 zone, user raises to 60 -> +20%; entering a 30 zone -> 36
    sla = make_sla()
    events = FakeEvents()
    self._active_with_ratio(sla, events, 60., 50.)
    assert abs(sla.dynamic_offset_ratio - 0.20) < 1e-6

    step(sla, events, cluster_mph=60., limit_mph=30.)
    assert sla.is_active
    assert abs(sla.dynamic_offset_ratio - 0.20) < 1e-6  # ratio persists
    assert abs(sla.output_v_target - 36. * MPH) < 1e-6  # 30 * 1.2
    assert EventNameSP.speedLimitChanged in events.names

  def test_zone_change_snap_is_idempotent(self):
    # cruise helper snaps cluster to 36 (= 30 * 1.2); the resulting cluster
    # change must re-derive the SAME ratio, not wipe it (the v0.9.8 bug)
    sla = make_sla()
    events = FakeEvents()
    self._active_with_ratio(sla, events, 60., 50.)
    step(sla, events, cluster_mph=60., limit_mph=30.)   # zone change
    step(sla, events, cluster_mph=36., limit_mph=30.)   # helper snap arrives
    assert abs(sla.dynamic_offset_ratio - 0.20) < 1e-6
    assert abs(sla.output_v_target - 36. * MPH) < 1e-6

  def test_users_example_manual_decrease_re_locks_ratio(self):
    # in the 30 zone at 36 (+20%), user taps down to 33 -> ratio +10%;
    # next 40 zone -> target 44
    sla = make_sla()
    events = FakeEvents()
    self._active_with_ratio(sla, events, 60., 50.)
    step(sla, events, cluster_mph=60., limit_mph=30.)
    step(sla, events, cluster_mph=36., limit_mph=30.)
    release(sla, ButtonType.decelCruise)               # user taps down...
    step(sla, events, cluster_mph=33., limit_mph=30.)  # ...and the cluster follows
    assert abs(sla.dynamic_offset_ratio - 0.10) < 1e-6

    step(sla, events, cluster_mph=33., limit_mph=40.)
    assert abs(sla.output_v_target - 44. * MPH) < 1e-6

  def test_negative_ratio(self):
    sla = make_sla()
    events = FakeEvents()
    self._active_with_ratio(sla, events, 45., 50.)
    assert abs(sla.dynamic_offset_ratio - (-0.10)) < 1e-6
    step(sla, events, cluster_mph=45., limit_mph=30.)
    assert abs(sla.output_v_target - 27. * MPH) < 1e-6

  def test_ratio_clamped(self):
    # 40 mph set in a 20 zone would be +100%; clamp to +50% -> target 30
    sla = make_sla()
    events = FakeEvents()
    self._active_with_ratio(sla, events, 40., 20.)
    assert abs(sla.dynamic_offset_ratio - RATIO_LIMIT) < 1e-6
    assert abs(sla.output_v_target - 30. * MPH) < 1e-6

  def test_limit_dropout_holds_last_target(self):
    sla = make_sla()
    events = FakeEvents()
    self._active_with_ratio(sla, events, 60., 50.)
    step(sla, events, cluster_mph=60., limit_mph=50., has_limit=False)
    assert sla.is_active
    assert abs(sla.output_v_target - 60. * MPH) < 1e-6


class TestDeactivation:
  def test_long_disable_resets(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=50., limit_mph=45.)
    step(sla, events, long_enabled=False)
    assert sla.state == SpeedLimitAssistState.disabled
    assert sla.dynamic_offset_ratio == 0.0
    assert sla.output_v_target == V_CRUISE_UNSET

  def test_presses_while_active_are_plain_adjustments(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=50., limit_mph=45.)
    release(sla, ButtonType.accelCruise)
    step(sla, events, cluster_mph=46., limit_mph=45., n=3)
    assert sla.state in ACTIVE_STATES
    assert sla._hold_deadline["plus"] == 0.

  def test_adapting_when_well_above_target(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=50., limit_mph=45.)
    # zone drops to 25 while still doing 50 -> adapting
    step(sla, events, cluster_mph=50., limit_mph=25., v_ego_mph=50.)
    assert sla.state == SpeedLimitAssistState.adapting
    assert sla.is_active


class TestGasGate:
  """v3.4.5: the gate is now DEFINED as "the ramp is holding the set speed below
  the current zone's target". It no longer models its own coast envelope, so
  there is no second, independent opinion about when to start slowing. Detailed
  coverage lives in test_sla_cruise_ramp.py::TestGasGate; these pin the contract.
  """

  def _settled_45(self, sla, events):
    activate(sla, events, cluster_mph=45., limit_mph=45.)
    step(sla, events, cluster_mph=45., limit_mph=45., n=BUTTON_INTENT_FRAMES + 2)

  def test_gate_requires_the_ramp_to_be_holding_speed_down(self):
    # v3.4.8: also requires the car to actually be ABOVE the ramp target, so
    # this runs long enough for v_ego to fall behind it.
    sla = make_sla()
    events = FakeEvents()
    self._settled_45(sla, events)
    assert not sla.gas_gate_active
    step(sla, events, cluster_mph=45., limit_mph=45., next_limit_mph=30.,
         next_dist=120., n=60)
    assert sla.gas_gate_active

  def test_no_gate_for_higher_zone(self):
    sla = make_sla()
    events = FakeEvents()
    self._settled_45(sla, events)
    step(sla, events, cluster_mph=45., limit_mph=45., next_limit_mph=55.,
         next_dist=50., n=CONFIRM_N + 4)
    assert not sla.gas_gate_active

  def test_no_gate_when_inactive(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)  # preActive, never confirmed
    step(sla, events, cluster_mph=40., limit_mph=45., next_limit_mph=35., next_dist=50.)
    assert not sla.gas_gate_active

  def test_gate_clears_on_zone_entry(self):
    sla = make_sla()
    events = FakeEvents()
    self._settled_45(sla, events)
    step(sla, events, cluster_mph=45., limit_mph=45., next_limit_mph=30.,
         next_dist=120., n=60)
    assert sla.gas_gate_active
    # boundary crossed: ahead info gone, current limit is now 30 -> ramp re-seeds
    step(sla, events, cluster_mph=45., limit_mph=30., v_ego_mph=38.)
    assert not sla.gas_gate_active
    assert sla.is_active
