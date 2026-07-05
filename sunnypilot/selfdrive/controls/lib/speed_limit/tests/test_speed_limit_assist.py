"""
FunnyPilot v3.2.6e — Speed Limit Assist tests (tap-to-adopt + ratio carryover).

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
  SpeedLimitAssist, ACTIVE_STATES, DISABLED_GUARD_PERIOD, RATIO_LIMIT, TAP_MAX_DURATION, V_CRUISE_UNSET,
)

ButtonType = car.CarState.ButtonEvent.Type
EventNameSP = custom.OnroadEventSP.EventName
SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState

MPH = CV.MPH_TO_MS
GUARD_FRAMES = int(DISABLED_GUARD_PERIOD / DT_MDL) + 2


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


def tap_minus(sla):
  sla.update_car_state(make_button_cs(ButtonType.decelCruise, True))
  sla.update_car_state(make_button_cs(ButtonType.decelCruise, False))


def step(sla, events, cluster_mph=50., limit_mph=45., long_enabled=True, has_limit=True, v_ego_mph=None, n=1):
  if v_ego_mph is None:
    v_ego_mph = cluster_mph
  for _ in range(n):
    sla.update(long_enabled, False, v_ego_mph * MPH, 0.0, cluster_mph * MPH,
               limit_mph * MPH if has_limit else 0.0, limit_mph * MPH, has_limit, 0.0, events)


def arm(sla, events, **kw):
  step(sla, events, n=GUARD_FRAMES, **kw)
  assert sla.state == SpeedLimitAssistState.inactive
  events.clear()


class TestArming:
  def test_initial_state(self):
    sla = make_sla()
    assert sla.state == SpeedLimitAssistState.disabled
    assert sla.output_v_target == V_CRUISE_UNSET

  def test_arms_after_guard_period(self):
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events)
    assert not sla.is_active

  def test_mode_off_stays_disabled(self):
    sla = make_sla(mode=Mode.warning)
    events = FakeEvents()
    step(sla, events, n=GUARD_FRAMES)
    assert sla.state == SpeedLimitAssistState.disabled

  def test_no_auto_activation(self):
    # even with cluster == limit, SLA must never activate without a tap
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events, cluster_mph=45., limit_mph=45.)
    step(sla, events, cluster_mph=45., limit_mph=45., n=100)
    assert not sla.is_active


class TestTapToAdopt:
  def test_tap_activates_and_adopts_set_speed(self):
    # 50 mph set in a 45 mph zone: tap-down -> active at ~+11%, target == 50
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events)
    tap_minus(sla)
    step(sla, events)
    assert sla.is_active
    assert abs(sla.dynamic_offset_ratio - (50. - 45.) / 45.) < 1e-6
    # the target IS the adopted set speed: zero speed change on activation
    assert abs(sla.output_v_target - 50. * MPH) < 1e-6
    assert EventNameSP.speedLimitActive in events.names

  def test_tap_without_limit_does_nothing(self):
    sla = make_sla()
    events = FakeEvents()
    step(sla, events, has_limit=False, limit_mph=0., n=GUARD_FRAMES)
    assert sla.state == SpeedLimitAssistState.inactive
    tap_minus(sla)
    step(sla, events, has_limit=False, limit_mph=0.)
    assert not sla.is_active

  def test_long_press_does_not_activate(self):
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events)
    sla.update_car_state(make_button_cs(ButtonType.decelCruise, True))
    sla._press_t["minus"] = time.monotonic() - (TAP_MAX_DURATION + 0.1)  # simulate a held press
    sla.update_car_state(make_button_cs(ButtonType.decelCruise, False))
    step(sla, events)
    assert not sla.is_active

  def test_stale_tap_expires(self):
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events)
    tap_minus(sla)
    sla._tap_deadline["minus"] = time.monotonic() - 0.01  # expired
    step(sla, events)
    assert not sla.is_active

  def test_adoption_ratio_clamped(self):
    # 40 mph set in a 20 zone would be +100%; clamp to +50% -> target 30
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events, cluster_mph=40., limit_mph=20.)
    tap_minus(sla)
    step(sla, events, cluster_mph=40., limit_mph=20.)
    assert sla.is_active
    assert abs(sla.dynamic_offset_ratio - RATIO_LIMIT) < 1e-6
    assert abs(sla.output_v_target - 30. * MPH) < 1e-6


class TestRatioCarryover:
  def _activate(self, sla, events, cluster_mph, limit_mph):
    arm(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph)
    tap_minus(sla)
    step(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph)
    assert sla.is_active
    events.clear()

  def test_users_example_60_in_50_then_30_zone(self):
    # 60 in a 50 -> +20%; entering a 30 zone -> target 36
    sla = make_sla()
    events = FakeEvents()
    self._activate(sla, events, 60., 50.)
    assert abs(sla.dynamic_offset_ratio - 0.20) < 1e-6

    # zone changes to 30; cluster hasn't been snapped yet (still 60)
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
    self._activate(sla, events, 60., 50.)
    step(sla, events, cluster_mph=60., limit_mph=30.)   # zone change
    step(sla, events, cluster_mph=36., limit_mph=30.)   # helper snap arrives
    assert abs(sla.dynamic_offset_ratio - 0.20) < 1e-6
    assert abs(sla.output_v_target - 36. * MPH) < 1e-6

  def test_users_example_manual_decrease_re_locks_ratio(self):
    # in the 30 zone at 36 (+20%), user taps down to 33 -> ratio +10%;
    # next 40 zone -> target 44
    sla = make_sla()
    events = FakeEvents()
    self._activate(sla, events, 60., 50.)
    step(sla, events, cluster_mph=60., limit_mph=30.)
    step(sla, events, cluster_mph=36., limit_mph=30.)
    # user decreases set speed to 33
    step(sla, events, cluster_mph=33., limit_mph=30.)
    assert abs(sla.dynamic_offset_ratio - 0.10) < 1e-6

    # entering a 40 zone honors the new +10%
    step(sla, events, cluster_mph=33., limit_mph=40.)
    assert abs(sla.output_v_target - 44. * MPH) < 1e-6

  def test_negative_ratio(self):
    # set speed below the limit carries a negative offset
    sla = make_sla()
    events = FakeEvents()
    self._activate(sla, events, 45., 50.)
    assert abs(sla.dynamic_offset_ratio - (-0.10)) < 1e-6
    step(sla, events, cluster_mph=45., limit_mph=30.)
    assert abs(sla.output_v_target - 27. * MPH) < 1e-6

  def test_limit_dropout_holds_last_target(self):
    # current zone becomes invalid: final_last holds, SLA stays active
    sla = make_sla()
    events = FakeEvents()
    self._activate(sla, events, 60., 50.)
    step(sla, events, cluster_mph=60., limit_mph=50., has_limit=False)
    assert sla.is_active
    assert abs(sla.output_v_target - 60. * MPH) < 1e-6


class TestDeactivation:
  def test_long_disable_resets(self):
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events)
    tap_minus(sla)
    step(sla, events)
    assert sla.is_active
    step(sla, events, long_enabled=False)
    assert sla.state == SpeedLimitAssistState.disabled
    assert sla.dynamic_offset_ratio == 0.0
    assert sla.output_v_target == V_CRUISE_UNSET

  def test_taps_while_active_are_ignored(self):
    # once active, cruise taps are plain speed adjustments (consumed by the
    # cruise helper); SLA must not treat them as activation gestures
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events)
    tap_minus(sla)
    step(sla, events)
    assert sla.is_active
    tap_minus(sla)
    step(sla, events, n=3)
    assert sla.state in ACTIVE_STATES
    assert sla._tap_deadline["minus"] == 0.

  def test_adapting_when_well_above_target(self):
    sla = make_sla()
    events = FakeEvents()
    arm(sla, events, cluster_mph=50., limit_mph=45.)
    tap_minus(sla)
    step(sla, events, cluster_mph=50., limit_mph=45.)
    assert sla.state == SpeedLimitAssistState.active
    # zone drops to 25 (target 27.8) while still doing 50 -> adapting
    step(sla, events, cluster_mph=50., limit_mph=25., v_ego_mph=50.)
    assert sla.state == SpeedLimitAssistState.adapting
    assert sla.is_active
