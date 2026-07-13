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
  GATE_COAST_ACCEL, GATE_TIME_BUFFER, V_CRUISE_UNSET,
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
  """Engage + confirm in the arrow's direction, then deliver the snap."""
  engage(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph)
  if not sla.is_active:  # set == limit auto-activates during engage
    assert sla.state == SpeedLimitAssistState.preActive
    release(sla, ButtonType.accelCruise if cluster_mph < limit_mph else ButtonType.decelCruise)
    step(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph)
  assert sla.is_active
  # cruise helper snaps the set speed to the limit on the became-active edge
  step(sla, events, cluster_mph=limit_mph, limit_mph=limit_mph)
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


class TestPreActiveConfirm:
  def test_up_confirm_activates(self):
    # 40 set in a 45 zone: up arrow; pressing UP within the window activates
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    release(sla, ButtonType.accelCruise)
    step(sla, events, cluster_mph=40., limit_mph=45.)
    assert sla.is_active
    assert sla.dynamic_offset_ratio == 0.0
    assert abs(sla.output_v_target - 45. * MPH) < 1e-6
    assert EventNameSP.speedLimitActive in events.names

  def test_down_confirm_activates(self):
    # 45 set in a 40 zone: down arrow; pressing DOWN activates
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=45., limit_mph=40.)
    release(sla, ButtonType.decelCruise)
    step(sla, events, cluster_mph=45., limit_mph=40.)
    assert sla.is_active
    assert abs(sla.output_v_target - 40. * MPH) < 1e-6

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

  def test_dialing_onto_limit_activates_from_inactive(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)
    step(sla, events, cluster_mph=40., limit_mph=45., n=WINDOW_FRAMES + 2)
    assert sla.state == SpeedLimitAssistState.inactive
    step(sla, events, cluster_mph=45., limit_mph=45.)
    assert sla.is_active


class TestRatioCarryover:
  def _active_with_ratio(self, sla, events, set_mph, limit_mph):
    activate(sla, events, cluster_mph=limit_mph, limit_mph=limit_mph)
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
    step(sla, events, cluster_mph=33., limit_mph=30.)  # user decreases
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
  """45 zone approaching a 35 zone: gate inside the coast envelope, never brake."""

  def _active_45(self, sla, events):
    activate(sla, events, cluster_mph=45., limit_mph=45.)

  @staticmethod
  def _envelope(v_ego_ms, target_ms):
    return (v_ego_ms ** 2 - target_ms ** 2) / (2.0 * GATE_COAST_ACCEL) + target_ms * GATE_TIME_BUFFER

  def test_gate_engages_inside_envelope(self):
    sla = make_sla()
    events = FakeEvents()
    self._active_45(sla, events)
    envelope = self._envelope(45. * MPH, 35. * MPH)
    step(sla, events, cluster_mph=45., limit_mph=45., next_limit_mph=35., next_dist=envelope - 5.)
    assert sla.gas_gate_active

  def test_no_gate_far_from_zone(self):
    sla = make_sla()
    events = FakeEvents()
    self._active_45(sla, events)
    envelope = self._envelope(45. * MPH, 35. * MPH)
    step(sla, events, cluster_mph=45., limit_mph=45., next_limit_mph=35., next_dist=envelope + 100.)
    assert not sla.gas_gate_active

  def test_no_gate_for_higher_zone(self):
    sla = make_sla()
    events = FakeEvents()
    self._active_45(sla, events)
    step(sla, events, cluster_mph=45., limit_mph=45., next_limit_mph=55., next_dist=50.)
    assert not sla.gas_gate_active

  def test_no_gate_when_inactive(self):
    sla = make_sla()
    events = FakeEvents()
    engage(sla, events, cluster_mph=40., limit_mph=45.)  # preActive, never confirmed
    step(sla, events, cluster_mph=40., limit_mph=45., next_limit_mph=35., next_dist=50.)
    assert not sla.gas_gate_active

  def test_no_gate_when_already_slow_enough(self):
    sla = make_sla()
    events = FakeEvents()
    self._active_45(sla, events)
    step(sla, events, cluster_mph=45., limit_mph=45., v_ego_mph=34., next_limit_mph=35., next_dist=50.)
    assert not sla.gas_gate_active

  def test_gate_target_honors_ratio(self):
    # +20% ratio: the upcoming 35 zone's target is 42, so at 43 mph the
    # envelope is short — barely over means the gate arms only very close in
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=50., limit_mph=50.)
    step(sla, events, cluster_mph=60., limit_mph=50.)  # ratio -> +20%
    next_target = 35. * MPH * 1.2
    envelope = self._envelope(60. * MPH, next_target)
    step(sla, events, cluster_mph=60., limit_mph=50., next_limit_mph=35., next_dist=envelope - 5.)
    assert sla.gas_gate_active
    step(sla, events, cluster_mph=60., limit_mph=50., next_limit_mph=35., next_dist=envelope + 50.)
    assert not sla.gas_gate_active

  def test_gate_clears_on_zone_entry(self):
    sla = make_sla()
    events = FakeEvents()
    self._active_45(sla, events)
    step(sla, events, cluster_mph=45., limit_mph=45., next_limit_mph=35., next_dist=100.)
    assert sla.gas_gate_active
    # boundary crossed: ahead info gone, current limit is now 35
    step(sla, events, cluster_mph=45., limit_mph=35., v_ego_mph=38.)
    assert not sla.gas_gate_active
    assert sla.is_active
