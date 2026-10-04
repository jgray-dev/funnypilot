"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from cereal import car
from types import SimpleNamespace as NS

from openpilot.common.constants import CV
from openpilot.sunnypilot.selfdrive.controls.lib.blinker_pause_lateral import BlinkerPauseLateral


class TestBlinkerPauseLateral:

  def setup_method(self):
    self.blinker_pause_lateral = BlinkerPauseLateral(NS(get_bool=lambda k: False))
    self._reset_states()

  def _reset_states(self):
    self.blinker_pause_lateral.enabled = True
    self.blinker_pause_lateral.is_metric = False
    self.blinker_pause_lateral.min_speed = 20  # MPH
    self.blinker_pause_lateral.reengage_delay = 0
    self.blinker_pause_lateral.blinker_off_timer = 0.0

    self.CS = car.CarState.new_message()
    self.CS.vEgo = 0
    self.CS.leftBlinker = False
    self.CS.rightBlinker = False

  def _test_should_blinker_pause_lateral(self, expected_results) -> None:
    for left in (True, False):
      for right in (True, False):
        # Isolate each combo: clear any carried-over unwind-hold state so this
        # exercises pure speed/blinker gating, not the post-blinker settle hold
        # (covered separately below).
        self.blinker_pause_lateral._blinker_was_on = False
        self.blinker_pause_lateral.model_settle.reset()
        self.blinker_pause_lateral.blinker_off_timer = 0.0

        self.CS.leftBlinker = left
        self.CS.rightBlinker = right

        result = self.blinker_pause_lateral.update(self.CS)
        assert result == expected_results[(left, right)]

  def test_below_min_speed_blinker(self):
    self.CS.vEgo = 4.5  # ~10 MPH

    expected_results = {
      (False, False): False,
      (True, False): True,
      (False, True): True,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

  def test_above_min_speed_blinker(self):
    self.CS.vEgo = 13.4  # ~30 MPH

    expected_results = {
      (False, False): False,
      (True, False): False,
      (False, True): False,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

  def test_just_below_min_speed(self):
    self.CS.vEgo = (20 * CV.MPH_TO_MS) - 0.01

    expected_results = {
      (False, False): False,
      (True, False): True,
      (False, True): True,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

  def test_disabled(self):
    self.blinker_pause_lateral.enabled = False
    self.CS.vEgo = 4.5  # ~10 MPH

    expected_results = {
      (False, False): False,
      (True, False): False,
      (False, True): False,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

  def test_metric_units_below_min_speed(self):
    self.blinker_pause_lateral.is_metric = True
    self.CS.vEgo = 5.0  # ~18 km/h

    expected_results = {
      (False, False): False,
      (True, False): True,
      (False, True): True,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

  def test_metric_units_above_threshold(self):
    self.blinker_pause_lateral.is_metric = True
    self.CS.vEgo = 6.0  # ~21.6 km/h

    expected_results = {
      (False, False): False,
      (True, False): False,
      (False, True): False,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

  def test_change_min_speed_threshold(self):
    self.blinker_pause_lateral.min_speed = 30  # MPH

    # below min speed
    self.CS.vEgo = 11.2  # ~25 MPH

    expected_results = {
      (False, False): False,
      (True, False): True,
      (False, True): True,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

    # above min speed
    self.CS.vEgo = 15.6  # ~35 MPH

    expected_results = {
      (False, False): False,
      (True, False): False,
      (False, True): False,
      (True, True): False
    }
    self._test_should_blinker_pause_lateral(expected_results)

  def test_no_model_cannot_unlock_even_with_straight_wheel(self):
    self.CS.vEgo = 4.5
    self.CS.leftBlinker = True
    assert self.blinker_pause_lateral.update(self.CS)
    self.CS.leftBlinker = False
    for _ in range(300):
      assert self.blinker_pause_lateral.update(self.CS)

  def test_curve_release_and_new_signal_restarts_dwell(self):
    from cereal import log
    model = log.ModelDataV2.new_message()
    model.action.desiredCurvature = .01
    model.orientationRate.t = [0., .5, 1.]
    model.orientationRate.z = [.05]*3
    self.CS.vEgo = 5.
    self.CS.steeringAngleDeg = 35.  # explicitly not a near-center wheel
    self.CS.leftBlinker = True
    assert self.blinker_pause_lateral.update(self.CS)
    self.CS.leftBlinker = False
    for i in range(7):
      now = 1+i*.05
      paused = self.blinker_pause_lateral.update(self.CS, model=model, now=now, received=now,
                                                stamp=i+1, valid=True, measured_curvature=.01)
      assert paused == (i < 6)
    self.CS.leftBlinker = True
    assert self.blinker_pause_lateral.update(self.CS)
    self.CS.vEgo = 30.  # accelerating above threshold cannot release a turn
    assert self.blinker_pause_lateral.update(self.CS)
    self.blinker_pause_lateral.enabled = False
    assert not self.blinker_pause_lateral.update(self.CS)
    assert not self.blinker_pause_lateral._blinker_was_on

  def test_guiding_a_consistent_curve_does_not_require_releasing_the_wheel(self):
    from cereal import log
    model = log.ModelDataV2.new_message()
    model.action.desiredCurvature = .004
    model.orientationRate.t = [0., .5, 1.]
    model.orientationRate.z = [.04]*3
    self.CS.vEgo = 8.
    self.CS.leftBlinker = True
    assert self.blinker_pause_lateral.update(self.CS)
    self.CS.leftBlinker = False
    self.CS.vEgo = 10.
    self.CS.steeringPressed = True
    self.CS.steeringTorque = -190.
    self.CS.steeringAngleDeg = -15.
    for i in range(7):
      now = 1+i*.05
      paused = self.blinker_pause_lateral.update(self.CS, model=model, now=now, received=now,
                                                stamp=i+1, valid=True, measured_curvature=.004)
      assert paused == (i < 6)

  def test_driver_and_model_disagreement_uses_bounded_fallback(self):
    from cereal import log
    model = log.ModelDataV2.new_message()
    model.action.desiredCurvature = .004
    model.orientationRate.t = [0., .5, 1.]
    model.orientationRate.z = [.04]*3
    self.CS.vEgo = 8.
    self.CS.leftBlinker = True
    assert self.blinker_pause_lateral.update(self.CS)
    self.CS.leftBlinker = False
    self.CS.vEgo = 10.
    self.CS.steeringPressed = True
    for i in range(15):
      now = 1+i*.05
      paused = self.blinker_pause_lateral.update(self.CS, model=model, now=now, received=now,
                                                stamp=i+1, valid=True, measured_curvature=-.004)
      assert paused == (i < 14)
    assert self.blinker_pause_lateral.release_reason == "timeout"


def paused_gate():
  gate = BlinkerPauseLateral(NS(get_bool=lambda key: False))
  gate.enabled, gate.min_speed = True, 20
  cs = car.CarState.new_message(vEgo=5., leftBlinker=True)
  assert gate.update(cs)
  cs.leftBlinker = False
  return gate, cs


def model_tick(gate, cs, now, **kw):
  model = NS(action=NS(desiredCurvature=.04), orientationRate=NS(t=[0., .5, 1.], z=[.2]*3),
             meta=NS(laneChangeState='off'))
  state = dict(model=model, now=now, received=now, stamp=round(now*1e9), valid=True, measured_curvature=-.04)
  state.update(kw)
  return gate.update(cs, **state)


def test_unstable_model_cannot_restart_absolute_fallback():
  gate, cs = paused_gate()
  cs.steeringAngleDeg = 60.
  cs.steeringPressed = True
  for i in range(67):
    assert model_tick(gate, cs, 1+i*.01)
    assert len(gate.model_settle.samples) == 0
  assert not model_tick(gate, cs, 1.67)
  assert gate.release_reason == 'timeout'


def test_signal_or_hazards_restart_fallback_but_speed_does_not():
  for hazards in (False, True):
    gate, cs = paused_gate()
    assert model_tick(gate, cs, 1.)
    assert model_tick(gate, cs, 1.6)
    cs.rightBlinker = True
    cs.leftBlinker = hazards
    cs.vEgo = 20.
    assert model_tick(gate, cs, 2.)
    assert gate.blinker_off_timer == 0.
    cs.rightBlinker = cs.leftBlinker = False
    assert model_tick(gate, cs, 2.5)
    assert model_tick(gate, cs, 3.16)
    assert not model_tick(gate, cs, 3.18)


def test_configured_delay_remains_minimum_for_both_paths():
  for consistent in (False, True):
    gate, cs = paused_gate()
    gate.reengage_delay = 2.
    for i in range(40):
      assert model_tick(gate, cs, 1+i*.05, measured_curvature=.04 if consistent else -.04)
    assert not model_tick(gate, cs, 3., measured_curvature=.04 if consistent else -.04)
    assert gate.release_reason == ('model' if consistent else 'timeout')


def test_expired_fallback_waits_for_valid_current_model_then_releases():
  invalid = [dict(valid=False), dict(received=0.), dict(received=1.), dict(received=10.),
             dict(stamp=0), dict(model=None), dict(measured_curvature=float('nan')),
             dict(model=NS(action=NS(desiredCurvature=float('nan')), orientationRate=NS(t=[0., .5, 1.], z=[.2]*3),
                           meta=NS(laneChangeState='off')))]
  for bad in invalid:
    gate, cs = paused_gate()
    assert model_tick(gate, cs, 1.)
    assert model_tick(gate, cs, 2., **bad)
    assert gate._blinker_was_on
    assert not model_tick(gate, cs, 2.01)
    assert gate.release_reason == 'timeout'


def test_model_maneuver_and_malformed_plan_still_block_timeout():
  for state, times in [('laneChangeStarting', [0., .5, 1.]), ('off', [0., .5, .4])]:
    gate, cs = paused_gate()
    assert model_tick(gate, cs, 1.)
    m = NS(action=NS(desiredCurvature=.04), orientationRate=NS(t=times, z=[.2]*3), meta=NS(laneChangeState=state))
    assert model_tick(gate, cs, 2., model=m)
    assert not model_tick(gate, cs, 2.01)


def test_clock_rollback_or_invalid_clock_cannot_release_pause():
  for bad in (float('nan'), float('inf'), None, .5):
    gate, cs = paused_gate()
    assert model_tick(gate, cs, 1.)
    assert model_tick(gate, cs, 1.6)
    # Explicit clock injection avoids altering the simulated producer stamp.
    assert gate.update(cs, now=bad)
    restart = .5 if bad == .5 else 1.7
    assert model_tick(gate, cs, restart)
    assert not model_tick(gate, cs, restart+.68)


def test_timeout_does_not_require_vehicle_to_be_near_standstill():
  for speed in (0., 1., 10., 20.):
    gate, cs = paused_gate()
    cs.vEgo = speed
    assert model_tick(gate, cs, 1.)
    assert not model_tick(gate, cs, 1.68)


def test_pre_signal_off_model_age_cannot_shorten_minimum():
  gate, cs = paused_gate()
  # The first cached model was received .1 seconds before the signal cleared.
  for i in range(6):
    now = 1+i*.05
    assert model_tick(gate, cs, now, received=now-.1, stamp=i+1, measured_curvature=.04)
  assert not model_tick(gate, cs, 1.3, received=1.2, stamp=7, measured_curvature=.04)
  assert gate.release_reason == 'model'
