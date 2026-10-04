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
    for i in range(15):
      now = 1+i*.05
      paused = self.blinker_pause_lateral.update(self.CS, model=model, now=now, received=now,
                                                stamp=i+1, valid=True, measured_curvature=.01)
      assert paused == (i < 14)
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
    for i in range(15):
      now = 1+i*.05
      paused = self.blinker_pause_lateral.update(self.CS, model=model, now=now, received=now,
                                                stamp=i+1, valid=True, measured_curvature=.004)
      assert paused == (i < 14)

  def test_driver_and_model_disagreement_still_prevents_handover(self):
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
    for i in range(30):
      now = 1+i*.05
      assert self.blinker_pause_lateral.update(self.CS, model=model, now=now, received=now,
                                               stamp=i+1, valid=True, measured_curvature=-.004)
