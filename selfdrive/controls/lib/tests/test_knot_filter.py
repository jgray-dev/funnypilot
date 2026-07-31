"""FunnyPilot v3.4.9 — KnotFilter invariants (import-light, stdlib only).

The contract, in the order it matters:
  1. Content the model's plan PREDICTED passes through EXACTLY. This is the
     property the v3.2.12 EMA could not have and the reason it felt sloppy.
  2. What is damped is bounded, in physical units, and provably converges.
  3. A large surprise is not damped meaningfully — decisive onsets stay
     decisive (the v3.3.2 post-mortem is still binding).

Each test names the mutation it guards.
"""
import math

from openpilot.selfdrive.controls.lib.knot_filter import (
  KnotFilter, BETA_MIN, CARRY, DEV_MAX_LAT_ACCEL, N_FULL_LAT_ACCEL, V_REF_MIN,
)

V = 30.0  # m/s
DT_MDL = 0.05


def curv_for(lat_accel, v=V):
  return lat_accel / (v * v)


class TestPredictedContentIsUntouched:
  def test_perfect_prediction_passes_through_exactly(self):
    """MUTATION: damp the raw action instead of the innovation (i.e. become an
    EMA). A plan that predicted every knot must produce zero deviation."""
    f = KnotFilter()
    knots = [0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006]
    f.update(knots[0], V)
    for i in range(1, len(knots)):
      f.set_prediction(knots[i])          # the plan called this one exactly
      out = f.update(knots[i], V)
      assert abs(out - knots[i]) < 1e-15
      assert f.deviation == 0.0

  def test_constant_rate_turn_in_has_no_lag(self):
    """The steady turn-in case the EMA lagged: equal deltas, plan agrees."""
    f = KnotFilter()
    c = 0.0
    step = curv_for(0.10)
    f.update(c, V)
    for _ in range(40):
      c += step
      f.set_prediction(c)
      assert abs(f.update(c, V) - c) < 1e-15


class TestUnpredictedContentIsSpread:
  def test_a_surprise_is_spread_over_several_frames(self):
    """MUTATION: set CARRY to 0. The whole remainder would then land on the
    very next knot, which is a one-frame delay, not smoothing."""
    f = KnotFilter()
    f.update(0.0, V)
    f.set_prediction(0.0)                 # the plan says "stay straight"
    target = curv_for(0.10)

    outs = [f.update(target, V)]
    for _ in range(6):
      f.set_prediction(target)            # from here the plan agrees
      outs.append(f.update(target, V))

    steps = [outs[0]] + [outs[i] - outs[i - 1] for i in range(1, len(outs))]
    assert outs[0] < target * (1.0 - BETA_MIN / 2)     # not delivered at once
    assert max(steps) < target * 0.75                  # peak step is reduced
    assert sum(1 for s in steps if s > target * 0.02) >= 3  # spread, not deferred

  def test_peak_rate_is_reduced_versus_raw(self):
    f = KnotFilter()
    f.update(0.0, V)
    f.set_prediction(0.0)
    target = curv_for(0.08)
    out_prev = 0.0
    peak = 0.0
    for i in range(8):
      f.set_prediction(target if i else 0.0)
      out = f.update(target, V)
      peak = max(peak, abs(out - out_prev))
      out_prev = out
    assert peak < target * 0.8, "frame-to-frame command change must shrink"

  def test_converges_to_the_model_desire(self):
    """MUTATION: raise CARRY to 1.0 (or above 1/(1-BETA_MIN)). [1] must be a
    contraction or the command can drift away from the plan indefinitely."""
    f = KnotFilter()
    f.update(0.0, V)
    f.set_prediction(0.0)
    target = curv_for(0.10)
    out = f.update(target, V)
    for _ in range(30):
      f.set_prediction(target)
      out = f.update(target, V)
    assert abs(out - target) < target * 1e-3
    assert (1.0 - BETA_MIN) * CARRY < 1.0


class TestBoundedDeviation:
  def test_deviation_never_exceeds_the_lat_accel_cap(self):
    """MUTATION: drop the clamp. This bound is the whole answer to 'the car
    ends up somewhere the model did not want'."""
    f = KnotFilter()
    f.update(0.0, V)
    for i in range(200):
      # adversarial: alternate huge unpredicted swings
      f.set_prediction(0.0)
      raw = curv_for(3.0) * (1 if i % 2 else -1)
      out = f.update(raw, V)
      assert abs(out - raw) <= DEV_MAX_LAT_ACCEL / (V * V) + 1e-12
      assert f.deviation <= DEV_MAX_LAT_ACCEL + 1e-9

  def test_the_whole_schedule_is_in_lateral_accel_not_curvature(self):
    """MUTATION: express BETA/DEV in curvature. The same curvature deviation is
    an order of magnitude more felt at 30 m/s than at 10; every threshold here
    has to be a lateral acceleration or the filter is a different filter at
    every speed."""
    f_slow, f_fast = KnotFilter(), KnotFilter()
    for f, v in ((f_slow, 10.0), (f_fast, 30.0)):
      f.update(0.0, v)
      f.set_prediction(0.0)
      f.update(curv_for(0.3, v), v)  # same lat accel innovation at both speeds
    assert abs(f_slow._offset) > abs(f_fast._offset)  # differs in curvature
    assert abs(f_slow.deviation - f_fast.deviation) < 1e-9   # identical in m/s^2
    assert abs(f_slow.beta - f_fast.beta) < 1e-9

  def test_speed_floor_keeps_bounds_finite(self):
    f = KnotFilter()
    f.update(0.0, 0.0)
    f.set_prediction(0.0)
    out = f.update(0.05, 0.0)
    assert math.isfinite(out)
    assert abs(out - 0.05) <= DEV_MAX_LAT_ACCEL / (V_REF_MIN ** 2) + 1e-12


class TestLargeSurprisesArePassedThrough:
  def test_evasive_step_is_barely_damped(self):
    """MUTATION: make beta constant at BETA_MIN. An evasive command must not be
    held back — 'no partial steering in the space before the 5'."""
    f = KnotFilter()
    f.update(0.0, V)
    f.set_prediction(0.0)
    big = curv_for(5.0 * N_FULL_LAT_ACCEL)
    out = f.update(big, V)
    assert f.beta > 0.99
    assert abs(out - big) < 1e-12          # beta == 1 -> offset is exactly 0

  def test_gain_is_monotone_in_innovation(self):
    prev = 0.0
    for a in (0.0, 0.05, 0.1, 0.15, 0.2, 0.4):
      f = KnotFilter()
      f.update(0.0, V)
      f.set_prediction(0.0)
      f.update(curv_for(a), V)
      assert f.beta >= prev - 1e-12
      prev = f.beta


class TestDegradesSafely:
  def test_missing_plan_falls_back_to_the_previous_action(self):
    """No plan sample available -> a plain constant-curvature process model,
    i.e. degrading TOWARD a one-frame difference, never past it."""
    f = KnotFilter()
    f.update(0.001, V)
    f.set_prediction(None)
    out = f.update(0.001, V)
    assert abs(out - 0.001) < 1e-15  # no change -> no innovation -> no damping

  def test_first_knot_after_reset_passes_through(self):
    f = KnotFilter()
    assert f.update(0.007, V) == 0.007

  def test_non_finite_knot_is_returned_untouched_and_keeps_state(self):
    f = KnotFilter()
    f.update(0.0, V)
    f.set_prediction(0.0)
    f.update(curv_for(0.1), V)
    off = f._offset
    out = f.update(float('nan'), V)
    assert math.isnan(out)
    assert f._offset == off

  def test_reset_clears_everything(self):
    f = KnotFilter()
    f.update(0.0, V)
    f.set_prediction(0.0)
    f.update(curv_for(0.1), V)
    assert f._offset != 0.0
    f.reset()
    assert f._offset == 0.0 and f.deviation == 0.0
    assert f.update(0.004, V) == 0.004

  def test_disabled_is_an_exact_passthrough(self):
    f = KnotFilter(enabled=False)
    f.update(0.0, V)
    f.set_prediction(0.0)
    assert f.update(curv_for(1.0), V) == curv_for(1.0)
    assert f.deviation == 0.0


class TestPathErrorIsNegligible:
  def test_worst_case_lateral_displacement_is_millimetres(self):
    """The user-facing claim, made checkable: even PINNED at the cap for the
    whole convergence, the path error is a few millimetres."""
    # convergence time: offset decays by (1-BETA_MIN)*CARRY per model frame
    frames = math.ceil(math.log(0.05) / math.log((1.0 - BETA_MIN) * CARRY))
    t = frames * DT_MDL
    displacement = 0.5 * DEV_MAX_LAT_ACCEL * t * t
    assert displacement < 0.02, f"{displacement * 1000:.1f} mm"
