"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from cereal import car

from openpilot.common.constants import CV
from openpilot.sunnypilot.selfdrive.controls.lib.blinker_pause_lateral import BlinkerPauseLateral


class TestBlinkerPauseLateral:

  def setup_method(self):
    self.blinker_pause_lateral = BlinkerPauseLateral()
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
        self.blinker_pause_lateral._unwind_settle_timer = 0.0
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

  def test_unwind_settle_hold(self):
    # v3.2.1e: after the blinker turns off, lateral stays paused until the wheel
    # has been within UNWIND_THRESHOLD_DEG of center continuously for
    # UNWIND_SETTLE_TIME, then re-engages.
    from openpilot.sunnypilot.selfdrive.controls.lib.blinker_pause_lateral import (
      UNWIND_SETTLE_TIME, UNWIND_THRESHOLD_DEG,
    )
    self.CS.vEgo = 4.5  # below min speed

    # blinker on → paused
    self.CS.leftBlinker = True
    assert self.blinker_pause_lateral.update(self.CS) is True

    # blinker off but wheel still past threshold → stays paused, no settle yet
    self.CS.leftBlinker = False
    self.CS.steeringAngleDeg = UNWIND_THRESHOLD_DEG + 5.0
    for _ in range(int((UNWIND_SETTLE_TIME + 0.5) / 0.01)):
      assert self.blinker_pause_lateral.update(self.CS) is True

    # wheel near center → held for the first part of the settle window
    self.CS.steeringAngleDeg = 0.0
    assert self.blinker_pause_lateral.update(self.CS) is True
    # keep centered past the settle time → eventually re-engages (returns False)
    released = False
    for _ in range(int(UNWIND_SETTLE_TIME / 0.01) + 5):
      if self.blinker_pause_lateral.update(self.CS) is False:
        released = True
        break
    assert released

  def test_unwind_settle_resets_on_excursion(self):
    # Crossing center briefly (the middle of an S-curve, wheel passing through
    # 0° on its way to the opposite lock) must NOT release the pause.
    from openpilot.sunnypilot.selfdrive.controls.lib.blinker_pause_lateral import (
      UNWIND_SETTLE_TIME,
    )
    self.CS.vEgo = 4.5
    self.CS.leftBlinker = True
    self.blinker_pause_lateral.update(self.CS)
    self.CS.leftBlinker = False

    # near center for almost the full settle window
    self.CS.steeringAngleDeg = 0.0
    for _ in range(int(UNWIND_SETTLE_TIME / 0.01) - 5):
      assert self.blinker_pause_lateral.update(self.CS) is True
    # excursion to the other half of the S resets the settle timer
    self.CS.steeringAngleDeg = 40.0
    assert self.blinker_pause_lateral.update(self.CS) is True
    # back near center: a brief moment is not enough — still paused
    self.CS.steeringAngleDeg = 0.0
    assert self.blinker_pause_lateral.update(self.CS) is True

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
