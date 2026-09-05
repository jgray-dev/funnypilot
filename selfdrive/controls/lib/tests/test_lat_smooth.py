"""FunnyPilot v3.3.6 — LatSmoother invariants (import-light, stdlib only).

The contract: the validated 3.1.0e delta/5 TIMING, exactly — every knot value
reached on the validated schedule, flats never creep — with the in-period
SHAPE smoothed to a C1 monotone spline (SPLINE, default). LINEAR remains the
bit-exact validated ramp.
"""
import math

from openpilot.selfdrive.controls.lib.lat_smooth import LatSmoother, T_MODEL, PHASE_LEAD, HEALTH_FULL, LINEAR, SPLINE

DT = 0.01  # 100 Hz


def run_knot(s, knot, t0, n_frames=5, next_est=None):
  """Feed one 20 Hz knot then the remaining control frames; return the 5 outputs."""
  outs = [s.update(knot, True, t0, next_est)]
  for i in range(1, n_frames):
    outs.append(s.update(knot, False, t0 + i * DT))
  return outs


class TestLatSmootherLinear:
  def test_validated_delta5_schedule(self):
    # at a healthy 100 Hz, the five frames after a knot emit
    # prev + (0.2, 0.4, 0.6, 0.8, 1.0) * delta — bit-compatible with 3.1.0e+
    s = LatSmoother(method=LINEAR)
    s.reset(0.0)
    s.update(0.0, True, 0.0)  # establish the trajectory at 0
    outs = run_knot(s, 1.0, 0.05)
    expected = [0.2, 0.4, 0.6, 0.8, 1.0]
    for got, want in zip(outs, expected, strict=True):
      assert abs(got - want) < 1e-9

  def test_moves_every_frame(self):
    # the whole point: no frame between knots is ever flat
    s = LatSmoother(method=LINEAR)
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    prev = s.out
    for i in range(1, 5):
      out = s.update(1.0, False, 0.05 + i * DT)
      assert out - prev > 0.15  # ~delta/5 per frame
      prev = out

  def test_continuity_on_early_knot(self):
    # a knot arriving after only 2 control frames must not step the output:
    # prev becomes the last OUTPUT, not the stale old knot
    s = LatSmoother(method=LINEAR)
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    s.update(1.0, False, 0.06)  # out = 0.4
    before = s.out
    s.update(2.0, True, 0.07)   # early knot; first emit = before + 0.2*(2.0-before)
    step = s.out - before
    assert abs(step - PHASE_LEAD * (2.0 - before)) < 1e-9

  def test_cadence_independent_of_control_rate(self):
    # a 50 Hz control loop lands on the same time-anchored line
    s = LatSmoother(method=LINEAR)
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    out = s.update(1.0, False, 0.05 + 0.02)  # 20 ms later
    assert abs(out - (PHASE_LEAD + 0.02 / T_MODEL) * 1.0) < 1e-9


class TestLatSmootherSpline:
  def test_default_method_is_spline(self):
    assert LatSmoother().method == SPLINE

  def test_constant_ramp_matches_validated_schedule(self):
    # KEY compatibility invariant: on a constant-rate maneuver (equal deltas)
    # the carried slope equals the secant and the spline collapses to the
    # validated delta/5 line — bit-compatible where months of use validated it.
    s = LatSmoother()
    s.reset(0.0)
    run_knot(s, 0.0, 0.0)
    run_knot(s, 0.1, 0.05, next_est=0.2)   # ramp: deltas all 0.1
    outs = run_knot(s, 0.2, 0.10, next_est=0.3)
    expected = [0.1 + f * 0.1 for f in (0.2, 0.4, 0.6, 0.8, 1.0)]
    for got, want in zip(outs, expected, strict=True):
      assert abs(got - want) < 1e-9

  def test_knot_values_reached_exactly_on_time(self):
    # NO redistribution: whatever the shaping does, each knot value is emitted
    # exactly at the end of its period — onset timing identical to LINEAR.
    s = LatSmoother()
    s.reset(0.0)
    knots = [0.0, 0.02, 0.07, 0.07, 0.03, -0.01]
    for k, knot in enumerate(knots):
      outs = run_knot(s, knot, k * 0.05)
      assert abs(outs[-1] - knot) < 1e-12

  def test_flat_desire_never_creeps(self):
    # the v3.2.12 EMA failure mode ("a 2.5 in the space before the 5"): with
    # the desire flat, the output must sit EXACTLY on it — even when the plan
    # lookahead already announces a big upcoming turn — until the knot moves.
    s = LatSmoother()
    s.reset(0.0)
    run_knot(s, 0.0, 0.0)
    for k in range(1, 4):
      outs = run_knot(s, 0.0, k * 0.05, next_est=0.1)
      assert all(o == 0.0 for o in outs)
    # then the turn knot arrives and the output moves decisively
    outs = run_knot(s, 0.05, 0.20, next_est=0.1)
    assert outs[-1] == 0.05
    assert outs[0] > 0.0

  def test_moves_every_frame_and_monotone(self):
    s = LatSmoother()
    s.reset(0.0)
    run_knot(s, 0.0, 0.0)
    prev = 0.0
    outs = run_knot(s, 1.0, 0.05)
    for out in outs:
      assert out > prev  # strictly monotone toward the knot
      prev = out

  def test_output_always_within_knot_bracket(self):
    # extreme/adversarial lookaheads must never push the output outside
    # [prev, cur] or keep it from landing on cur
    s = LatSmoother()
    s.reset(0.0)
    for k, (knot, est) in enumerate([(0.1, 10.0), (0.2, -10.0), (0.1, float("nan")),
                                     (0.0, 0.2), (-0.1, None), (-0.1, -0.1)]):
      start = s.out
      outs = run_knot(s, knot, k * 0.05, next_est=est)
      lo, hi = min(start, knot), max(start, knot)
      for out in outs:
        assert lo - 1e-12 <= out <= hi + 1e-12
      assert abs(outs[-1] - knot) < 1e-12

  def test_c1_no_rate_step_at_knot_on_accelerating_ramp(self):
    # where LINEAR kinks (per-frame step jumps 3x at the knot when the delta
    # grows 0.1 -> 0.3), the spline carries the realized slope across the
    # boundary: the first step of the new segment stays close to the last step
    # of the old one.
    lin_steps, spl_steps = [], []
    for method in (LINEAR, SPLINE):
      s = LatSmoother(method=method)
      s.reset(0.0)
      run_knot(s, 0.0, 0.0)
      outs1 = run_knot(s, 0.1, 0.05, next_est=0.4)
      last_step = outs1[-1] - outs1[-2]
      outs2 = run_knot(s, 0.4, 0.10, next_est=0.7)
      first_step = outs2[0] - outs1[-1]
      (lin_steps if method == LINEAR else spl_steps).append((last_step, first_step))
    lin_jump = lin_steps[0][1] / lin_steps[0][0]
    spl_jump = spl_steps[0][1] / spl_steps[0][0]
    assert lin_jump > 2.5           # the kink the linear scheme has
    assert spl_jump < lin_jump / 2  # the spline substantially removes it
    assert spl_jump > 0.5           # ...without stalling the wheel

  def test_settles_into_apex_when_plan_flattens(self):
    # deepening turn whose plan says the apex is here (next ~= cur): the last
    # in-period step eases off instead of arriving at full rate — the wheel
    # settles rather than jerks (the 3.2.2 SETTLE feel), while still landing
    # exactly on the knot on time.
    s = LatSmoother()
    s.reset(0.0)
    run_knot(s, 0.0, 0.0)
    run_knot(s, 0.1, 0.05, next_est=0.2)
    outs = run_knot(s, 0.2, 0.10, next_est=0.2)  # apex: plan flattens
    steps = [b - a for a, b in zip(outs, outs[1:], strict=False)]
    assert steps[-1] < steps[0]        # easing out...
    assert abs(outs[-1] - 0.2) < 1e-12  # ...but on time, on value
    assert all(st > 0.0 for st in steps)

  def test_deviation_from_linear_is_bounded(self):
    # shaping is sub-period only: the spline path never strays far from the
    # validated line (|g(a) - a| <= 0.25), so worst-case transient timing skew
    # inside one 50 ms segment stays a few ms — no maneuver-scale distortion.
    s = LatSmoother()
    s.reset(0.0)
    run_knot(s, 0.0, 0.0)
    run_knot(s, 0.3, 0.05, next_est=0.9)
    outs = run_knot(s, 0.9, 0.10, next_est=0.0)
    for i, out in enumerate(outs):
      alpha = PHASE_LEAD + i * DT / T_MODEL
      linear = 0.3 + alpha * (0.9 - 0.3)
      assert abs(out - linear) <= 0.25 * 0.6 + 1e-9

  def test_late_model_frame_holds_at_cur(self):
    # model stalls: alpha saturates at 1.0 and the output holds the knot value
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    for i in range(1, 30):  # 300 ms, no new knot
      out = s.update(1.0, False, 0.05 + i * DT)
    assert out == 1.0
    # and the segment after a hold restarts from zero slope, in-bracket
    outs = run_knot(s, 2.0, 0.40)
    assert all(1.0 <= o <= 2.0 for o in outs)
    assert abs(outs[-1] - 2.0) < 1e-12

  def test_nan_knot_holds_trajectory(self):
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    s.update(float("nan"), True, 0.10)  # bad model action: keep the old knot
    out = s.update(float("nan"), False, 0.11)
    assert math.isfinite(out)
    assert 0.0 <= out <= 1.0

  def test_reset_and_reengage(self):
    s = LatSmoother()
    s.update(1.0, True, 0.0)
    s.reset(3e-4)
    assert s.out == 3e-4
    # Re-engagement between model frames must use the same schedule as an
    # engagement on a model frame; the cached action is the first knot.
    out = s.update(5e-4, False, 1.0)
    reference = LatSmoother()
    reference.reset(3e-4)
    assert out == reference.update(5e-4, True, 1.0)
    assert 3e-4 < out < 5e-4
    for i in range(1, 5):
      before = out
      out = s.update(5e-4, False, 1.0 + i * DT)
      assert out > before
    assert abs(out - 5e-4) < 1e-12

  def test_health_counts_realized_frames(self):
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    for i in range(1, 5):
      s.update(0.0, False, i * DT)
    s.update(1.0, True, 0.05)
    assert s.health_frames == HEALTH_FULL

  def test_slope_carries_across_saturated_frame(self):
    # regression (v3.3.6): a single alpha-saturated frame (knot a touch late)
    # must carry the aimed exit slope into the next segment — not zero it and
    # ease-in from a standstill mid-maneuver. Only a real stall (2+ saturated
    # frames while holding cur) decays the carried slope.
    s = LatSmoother()
    s.reset(0.0)
    run_knot(s, 0.0, 0.0)
    run_knot(s, 0.1, 0.05, next_est=0.2)
    s.update(0.1, False, 0.104)  # one extra frame, alpha saturated past 1
    outs = run_knot(s, 0.2, 0.105, next_est=0.3)
    assert outs[0] - 0.1 > 0.015  # still ~the validated 0.2*delta first step

  def test_direction_reversal_restarts_from_zero_slope_in_bracket(self):
    # cur reverses while the wheel is mid-motion: the new segment must restart
    # monotone into the new bracket (no overshoot past the old position)
    s = LatSmoother()
    s.reset(0.0)
    run_knot(s, 0.0, 0.0)
    run_knot(s, 0.2, 0.05, next_est=0.4)
    start = s.out
    outs = run_knot(s, -0.1, 0.10, next_est=-0.2)
    for out in outs:
      assert -0.1 - 1e-12 <= out <= start + 1e-12
    assert abs(outs[-1] - (-0.1)) < 1e-12
