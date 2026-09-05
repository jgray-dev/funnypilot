"""FunnyPilot v3.5.4 — anticipatory turn limiting.

Import-light (numpy + ModelConstants), so it runs without acados or a car:
  python3 -m pytest selfdrive/controls/lib/tests/test_turn_limit.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../.."))

from openpilot.selfdrive.controls.lib.turn_limit import (
  predicted_lat_accel, limit_accel_in_turns, path_opening, TURN_LOOKAHEAD_T, _TURN_V_MIN,
  EXIT_A_X, _OPENING_A_MIN,
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


def unwinding(k0, k1, v=15.0, over_t=TURN_LOOKAHEAD_T):
  """A bend whose curvature falls linearly from k0 now to k1 at `over_t`."""
  vel = [v] * N
  rate = []
  for i in range(N):
    t = ModelConstants.T_IDXS[i]
    f = min(t / over_t, 1.0) if over_t > 0 else 1.0
    rate.append((k0 + (k1 - k0) * f) * v)
  return rate, vel


class TestPathOpening:
  """FunnyPilot v3.7.1 — is the road OPENING ahead? Zero on the entry and on a
  constant-radius arc BY CONSTRUCTION; positive only where the model can see
  the bend unwinding, which on a real corner is from the apex."""

  def test_a_straight_road_is_not_opening(self):
    rate, vel = straight(15.0)
    assert path_opening(rate, vel, 15.0) == 0.0

  def test_a_constant_radius_arc_is_not_opening(self):
    """MUTATION: return `1 - k_far/k_now` unclamped, or use the MIN over the
    window rather than the far end — noise-free constant curvature must read
    as exactly zero."""
    rate, vel = bend(0.01, v=15.0)
    assert path_opening(rate, vel, 15.0) == 0.0

  def test_the_entry_is_not_opening(self):
    """Curvature RISING ahead: the limit's original job. The clamp at zero is
    what keeps the allowance off the entry. MUTATION: take abs()."""
    rate, vel = unwinding(0.004, 0.012)
    assert path_opening(rate, vel, 15.0) == 0.0

  def test_the_exit_is_opening_and_by_how_much(self):
    rate, vel = unwinding(0.010, 0.005)
    op = path_opening(rate, vel, 15.0)
    assert 0.3 < op < 0.6, op       # about half the curvature gone by the far end
    rate, vel = unwinding(0.010, 0.0)
    assert path_opening(rate, vel, 15.0) > 0.75   # the far-end MEAN understates the very end, by design

  def test_it_is_monotone_in_how_far_the_road_opens(self):
    ops = [path_opening(*unwinding(0.010, k1), 15.0) for k1 in (0.010, 0.008, 0.006, 0.004, 0.002, 0.0)]
    assert ops[0] == 0.0
    assert all(b >= a for a, b in zip(ops, ops[1:], strict=False)), ops
    assert ops[-1] > ops[1]

  def test_a_gentle_bend_has_nothing_to_open(self):
    """Under _OPENING_A_MIN of lateral at our speed the sqrt ceiling is already
    wide, and the ratio would be noise on a near-straight road."""
    k = (_OPENING_A_MIN * 0.5) / 15.0 ** 2
    rate, vel = unwinding(k, 0.0)
    assert path_opening(rate, vel, 15.0) == 0.0

  def test_it_is_bounded_and_never_raises(self):
    assert path_opening([float('nan')] * N, [15.0] * N, 15.0) == 0.0
    assert path_opening([0.01 * 15] * N, [0.0] * N, 15.0) == 0.0
    assert path_opening([], [], 15.0) == 0.0
    assert path_opening(None, None, 15.0) == 0.0
    rate, vel = unwinding(0.010, -0.010)     # a reversal reads as fully open, no more
    assert path_opening(rate, vel, 15.0) <= 1.0
    assert path_opening(*unwinding(0.01, 0.0), 0.5) == 0.0   # crawl: no-op


class TestTheExitAllowance:
  """v3.7.1 — the third of the three pieces that made a corner exit a step.

  Below 20 m/s the total budget is 1.7 and the corner budget is 2.25, so the
  ceiling was ZERO for the whole arc of any governed corner. The allowance
  floors it at EXIT_A_X * opening — a floor on the ceiling, never a raise of
  the total budget.
  """

  def test_the_arithmetic_this_release_is_about(self):
    """Anti-vacuous: at the corner budget below 20 m/s the ceiling really is
    zero without the allowance. If this ever stops being true the allowance
    is no longer load-bearing and should be reconsidered."""
    out = limit_accel_in_turns(15.0, 0.0, [-3.5, 1.0], FakeCP(), a_y_predicted=2.25)
    assert out[1] == 0.0

  def test_no_opening_is_bit_identical_to_before(self):
    for a_y in (0.0, 1.0, 1.7, 2.25):
      base = limit_accel_in_turns(15.0, 0.0, [-3.5, 1.0], FakeCP(), a_y_predicted=a_y)
      same = limit_accel_in_turns(15.0, 0.0, [-3.5, 1.0], FakeCP(), a_y_predicted=a_y, opening=0.0)
      assert same == base

  def test_an_opening_road_lifts_the_ceiling_off_zero(self):
    """MUTATION: delete the floor."""
    out = limit_accel_in_turns(15.0, 0.0, [-3.5, 1.0], FakeCP(), a_y_predicted=2.25, opening=0.5)
    assert out[1] == 0.5 * EXIT_A_X
    full = limit_accel_in_turns(15.0, 0.0, [-3.5, 1.0], FakeCP(), a_y_predicted=2.25, opening=1.0)
    assert full[1] == EXIT_A_X

  def test_it_is_a_floor_not_an_addition(self):
    """Where the sqrt ceiling is already above the allowance, nothing changes.
    MUTATION: add instead of max()."""
    # A HIGH planner ceiling on purpose: the first cut used 1.0, which both the
    # floor and an addition clip down to, so the mutation survived a test that
    # could not see it. The ceiling here must not be the binder.
    base = limit_accel_in_turns(15.0, 0.0, [-3.5, 5.0], FakeCP(), a_y_predicted=1.0)
    out = limit_accel_in_turns(15.0, 0.0, [-3.5, 5.0], FakeCP(), a_y_predicted=1.0, opening=1.0)
    assert EXIT_A_X < base[1] < 5.0, base
    assert out == base

  def test_it_never_exceeds_the_planners_own_ceiling(self):
    out = limit_accel_in_turns(15.0, 0.0, [-3.5, 0.3], FakeCP(), a_y_predicted=2.25, opening=1.0)
    assert out[1] == 0.3

  def test_the_braking_floor_is_still_never_touched(self):
    out = limit_accel_in_turns(15.0, 0.0, [-3.5, 1.0], FakeCP(), a_y_predicted=2.25, opening=1.0)
    assert out[0] == -3.5

  def test_garbage_opening_is_no_allowance(self):
    for op in (float('nan'), None, "x", -1.0):
      out = limit_accel_in_turns(15.0, 0.0, [-3.5, 1.0], FakeCP(), a_y_predicted=2.25, opening=op)
      assert out[1] == 0.0

  def test_the_allowance_is_modest(self):
    """Around the car's own accel clip at corner speeds, not a launch."""
    assert 0.3 <= EXIT_A_X <= 0.85


class TestThePlannerWiresTheOpeningIn:
  """The planner imports acados and cannot be constructed off-device, so the
  call site is pinned on the AST — the same reason every other planner guard in
  this repo is. A ceiling floor that nothing passes an `opening` to is the
  v3.6.2 gas-gate story again: published, unread, green for years."""

  def _update_src(self):
    import ast
    import pathlib as _pl
    src = _pl.Path(__file__).resolve().parents[1] / "longitudinal_planner.py"
    tree = ast.parse(src.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "update")
    return ast.unparse(fn)

  def test_path_opening_is_computed_from_the_model_and_passed_to_the_limit(self):
    src = self._update_src()
    assert "path_opening(" in src, "the planner never asks whether the road is opening"
    call = next(ln for ln in src.splitlines() if "limit_accel_in_turns(" in ln)
    # the call spans two lines in the source; take the whole statement
    i = src.index("limit_accel_in_turns(")
    stmt = src[i:src.index(")", src.index("a_y_pred", i)) + 1]
    assert "opening" in stmt, f"the opening is computed but not handed to the limit: {call}"
