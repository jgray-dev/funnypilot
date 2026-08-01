"""FunnyPilot v3.5.4 — anticipatory turn limiting.

Import-light (numpy + ModelConstants), so it runs without acados or a car:
  python3 -m pytest selfdrive/controls/lib/tests/test_turn_limit.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../.."))

from openpilot.selfdrive.controls.lib.turn_limit import (
  predicted_lat_accel, limit_accel_in_turns, TURN_LOOKAHEAD_T, _TURN_V_MIN,
)
from openpilot.selfdrive.modeld.constants import ModelConstants

N = len(ModelConstants.T_IDXS)


class FakeCP:
  steerRatio = 13.0
  wheelbase = 2.8


def straight(v=25.0):
  return [0.0] * N, [v] * N


def bend(curvature, v=25.0, from_t=0.0):
  """A constant-curvature bend starting at `from_t` seconds."""
  vel = [v] * N
  rate = [curvature * v if ModelConstants.T_IDXS[i] >= from_t else 0.0 for i in range(N)]
  return rate, vel


class TestPredictedLatAccel:
  def test_a_straight_road_predicts_nothing(self):
    rate, vel = straight()
    assert predicted_lat_accel(rate, vel, 25.0) == 0.0

  def test_a_bend_predicts_curvature_times_our_speed_squared(self):
    # 0.002 rad/m at 25 m/s -> 1.25 m/s^2
    rate, vel = bend(0.002, v=25.0)
    assert abs(predicted_lat_accel(rate, vel, 25.0) - 0.002 * 25.0 ** 2) < 1e-6

  def test_it_is_evaluated_at_our_speed_not_the_models(self):
    """THE v3.4.9 TRAP. The model PLANS TO SLOW for a corner, so `rate * vel`
    — the accel the model intends to pull — reads as 'nothing to do' exactly
    where there is something to do. Recovering the geometry and evaluating it
    at OUR speed is what makes the number mean anything.

    MUTATION: use rate[i] * vel[i] instead of (rate[i]/vel[i]) * v_ego**2.
    """
    # the model has already planned to crawl through this bend at 10 m/s...
    rate, vel = bend(0.004, v=10.0)
    intended = 0.004 * 10.0 ** 2            # what the model plans to pull: 0.4
    # ...but we are still doing 30 and have not slowed yet
    out = predicted_lat_accel(rate, vel, 30.0)
    assert abs(out - 0.004 * 30.0 ** 2) < 1e-6
    assert out > intended * 5, "must reflect OUR speed, not the model's plan"

  def test_sign_does_not_matter(self):
    left, vel = bend(0.002)
    right, _ = bend(-0.002)
    assert abs(predicted_lat_accel(left, vel, 25.0) - predicted_lat_accel(right, vel, 25.0)) < 1e-9

  def test_it_takes_the_peak_over_the_window(self):
    rate, vel = bend(0.003, from_t=1.0)
    assert predicted_lat_accel(rate, vel, 25.0) > 0.0

  def test_a_bend_beyond_the_lookahead_is_ignored(self):
    """MUTATION: drop the lookahead break. A corner 200 m away would hold the
    car back on the straight leading to it."""
    rate, vel = bend(0.01, from_t=TURN_LOOKAHEAD_T + 1.0)
    assert predicted_lat_accel(rate, vel, 25.0) == 0.0

  def test_standstill_and_crawl_are_no_ops(self):
    rate, vel = bend(0.01)
    assert predicted_lat_accel(rate, vel, 0.0) == 0.0
    assert predicted_lat_accel(rate, vel, _TURN_V_MIN - 0.01) == 0.0

  def test_degenerate_input_is_a_no_op_not_a_crash(self):
    """Every failure path must return 0.0, which makes the caller bit-identical
    to the pre-v3.5.4 measured-angle-only behaviour."""
    rate, vel = bend(0.002)
    assert predicted_lat_accel([], [], 25.0) == 0.0
    assert predicted_lat_accel(rate, [], 25.0) == 0.0
    assert predicted_lat_accel(rate, vel, float('nan')) == 0.0
    assert predicted_lat_accel(rate, vel, float('inf')) == 0.0
    assert predicted_lat_accel([float('nan')] * N, vel, 25.0) == 0.0
    assert predicted_lat_accel(rate, [0.0] * N, 25.0) == 0.0
    assert predicted_lat_accel(None, None, 25.0) == 0.0


class TestLimitAccelInTurns:
  def test_no_prediction_is_bit_identical_to_before(self):
    """The default keeps the pre-v3.5.4 numbers exactly, so a model dropout
    cannot change how the car drives."""
    a = limit_accel_in_turns(25.0, 3.0, [-3.5, 1.0], FakeCP())
    b = limit_accel_in_turns(25.0, 3.0, [-3.5, 1.0], FakeCP(), 0.0)
    assert a == b

  def test_prediction_tightens_the_ceiling_before_the_wheel_turns(self):
    """THE WHOLE POINT: straight wheel, corner ahead, ceiling already coming
    down. The target here is deliberately generous (2.0) so the turn limit is
    what binds — with the fork's 70% A_CRUISE_MAX table the requested accel is
    often already below the limit, and a test using it would pass vacuously."""
    flat = limit_accel_in_turns(25.0, 0.0, [-3.5, 2.0], FakeCP(), 0.0)
    ahead = limit_accel_in_turns(25.0, 0.0, [-3.5, 2.0], FakeCP(), 1.9)
    assert flat[1] == 2.0, "target must not already be the binding constraint"
    assert ahead[1] < flat[1]

  def test_the_measured_angle_still_binds_on_its_own(self):
    """MUTATION: replace max(...) with just the predicted term. A quiet or
    dropped-out model would then OPEN the ceiling back up in a corner the wheel
    is plainly already in.

    Asserted ABSOLUTELY, not by comparing two calls: a relative assertion is
    satisfied by the mutant too, because it moves both sides together. (That is
    exactly how the first version of this test passed the mutation run.)
    """
    hard = limit_accel_in_turns(30.0, 8.0, [-3.5, 2.0], FakeCP(), 0.0)
    assert hard[1] < 2.0, "a hard measured angle must tighten the ceiling by itself"

  def test_a_loud_model_tightens_further(self):
    loose = limit_accel_in_turns(25.0, 0.0, [-3.5, 2.0], FakeCP(), 0.0)
    tight = limit_accel_in_turns(25.0, 0.0, [-3.5, 2.0], FakeCP(), 5.0)
    assert tight[1] < loose[1]

  def test_the_braking_floor_is_never_touched(self):
    """SAFETY: this bounds the accel CEILING only. Nothing here may command,
    delay or weaken braking."""
    for pred in (0.0, 1.0, 10.0, 1e6):
      out = limit_accel_in_turns(25.0, 5.0, [-3.5, 1.0], FakeCP(), pred)
      assert out[0] == -3.5

  def test_the_ceiling_never_goes_negative(self):
    out = limit_accel_in_turns(35.0, 25.0, [-3.5, 1.0], FakeCP(), 50.0)
    assert out[1] >= 0.0
