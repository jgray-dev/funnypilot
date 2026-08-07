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

import pytest

from openpilot.selfdrive.controls.lib import knot_filter as KF
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


class TestTheV362Retune:
  """FunnyPilot v3.6.2 — more of the lagd window spent on smoothness.

  Reported: corners that should be easy feel jittery. These are EXACT-VALUE
  tests, not inequalities: the filter is a closed-form recurrence, so given the
  inputs the outputs are fully determined and there is no reason to assert
  anything vaguer. If a future retune changes a number here, it should have to
  change the number deliberately rather than watch a range quietly widen.

  UNITS. The filter takes CURVATURE and converts internally with v^2, so every
  lateral-acceleration figure below is divided by VSQ before it goes in.
  Speed is pinned at V_REF_MIN so the filter's own speed floor cannot move the
  arithmetic out from under the assertions.
  """
  RAIL_A = 0.125                 # drive_helpers.MAX_TARGET_LAT_JERK * DT_MDL
  V = V_REF_MIN                  # 3.0 m/s; at or above the floor, so no clamp
  VSQ = V_REF_MIN * V_REF_MIN

  def _c(self, lat_accel):
    """lateral acceleration (m/s^2) -> curvature at the test speed."""
    return lat_accel / self.VSQ

  def _f(self):
    f = KnotFilter()
    f.set_prediction(0.0)
    return f

  def test_the_constants_are_what_the_analysis_assumed(self):
    """The response numbers quoted in the module docstring are only true for
    these values. Pinned so the prose and the code cannot drift apart."""
    assert KF.BETA_MIN == 0.22
    assert KF.N_FULL_LAT_ACCEL == 0.9
    assert KF.CARRY == 0.70
    assert KF.DEV_MAX_LAT_ACCEL == 0.22

  def test_it_is_a_contraction(self):
    """(1 - BETA_MIN) * CARRY < 1 is what stops the offset latching. This is
    the one relationship a retune must never break, whatever the values."""
    g = (1.0 - KF.BETA_MIN) * KF.CARRY
    assert g == pytest.approx(0.546)
    assert g < 1.0

  def test_a_perfectly_predicted_knot_is_bit_identical(self):
    """MUTATION: any change that makes the filter act on predicted motion
    turns it back into the v3.2.12 EMA, which had to be reverted."""
    f = self._f()
    for _ in range(20):
      assert f.update(0.0, self.V) == 0.0
      f.set_prediction(0.0)
    assert f.deviation == 0.0

  def test_an_innovation_at_or_above_n_full_is_untouched(self):
    """DECISIVE ONSETS STAY DECISIVE — the binding v3.3.2 requirement.
    beta reaches exactly 1.0, so the output is exactly the raw action."""
    for a in (KF.N_FULL_LAT_ACCEL, 1.2, 2.0, 5.0):
      f = self._f()
      n = self._c(a)
      assert f.update(n, self.V) == pytest.approx(n)
      assert f.beta == 1.0

  def test_the_rate_rail_keeps_exactly_a_third(self):
    """A change at the model's own per-frame ceiling is the sharpest ordinary
    adjustment, and is the one that is felt. beta = 0.22 + 0.78*(0.125/0.9)."""
    f = self._f()
    n = self._c(self.RAIL_A)
    out = f.update(n, self.V)
    assert f.beta == pytest.approx(0.22 + 0.78 * (self.RAIL_A / 0.9))
    assert f.beta == pytest.approx(0.32833, abs=1e-5)
    assert out == pytest.approx(n * f.beta)

  def test_it_damps_the_rail_harder_than_before_the_retune(self):
    """The point of the change, stated as a number. The old constants passed
    0.4458 of a rail-rate change on the frame it arrived; these pass 0.3283."""
    f = self._f()
    f.update(self._c(self.RAIL_A), self.V)
    old_beta = 0.30 + 0.70 * (self.RAIL_A / 0.6)
    assert old_beta == pytest.approx(0.44583, abs=1e-5)
    assert f.beta < old_beta

  def _sustained(self, frames=60):
    """A steady maneuver at the rail whose prediction is always one frame
    stale — the worst case, where every frame reads as a full surprise."""
    f = KnotFilter()
    raw = 0.0
    step = self._c(self.RAIL_A)
    for _ in range(frames):
      f.set_prediction(raw)
      raw += step
      f.update(raw, self.V)
    return f

  def test_sustained_unpredicted_motion_settles_at_a_known_lag(self):
    """63 ms is the price of the retune, and it is paid ONLY on motion the
    plan did not predict. Predicted motion still has exactly zero lag."""
    f = self._sustained()
    assert f.deviation == pytest.approx(0.1585, abs=3e-3)
    assert f.deviation / self.RAIL_A == pytest.approx(1.27, abs=0.03)

  def test_the_cap_stays_a_backstop_not_the_operating_point(self):
    """THE PROPERTY A RETUNE IS MOST LIKELY TO BREAK. If the deviation the
    filter actually reaches climbs onto DEV_MAX_LAT_ACCEL, the filter stops
    being a damper and becomes a hard clip -- which is both jerky and exactly
    the 'somewhere between the two places the model wanted' failure.

    MUTATION: raise BETA_MIN or CARRY without raising the cap."""
    f = self._sustained()
    assert f.deviation < KF.DEV_MAX_LAT_ACCEL * 0.85
    assert f.deviation / KF.DEV_MAX_LAT_ACCEL == pytest.approx(0.72, abs=0.03)

  def test_the_offset_decays_to_nothing_once_surprises_stop(self):
    """Exact geometric decay at the contraction factor. Six frames (300 ms)
    takes a charged offset to under 3% of the cap."""
    f = self._f()
    big = self._c(1.0)
    f.update(big, self.V)            # a big surprise to charge the offset
    f.set_prediction(big)
    devs = []
    for _ in range(6):
      f.update(big, self.V)          # perfectly predicted from here on
      f.set_prediction(big)
      devs.append(f.deviation)
    assert devs == sorted(devs, reverse=True)
    assert devs[-1] < 0.03 * KF.DEV_MAX_LAT_ACCEL

  def test_alternating_jitter_is_cut_to_a_known_fraction(self):
    """The reported symptom: a plan that revises itself every frame. The peak
    frame-to-frame command change is what the wheel actually does, and it
    drops to ~12% of the raw swing (17% before the retune)."""
    f = KnotFilter()
    swing = self._c(0.10)
    raw_vals, out_vals, raw = [], [], 0.0
    for i in range(24):
      f.set_prediction(raw)
      raw += swing if i % 2 == 0 else -swing
      out_vals.append(f.update(raw, self.V))
      raw_vals.append(raw)
    peak_raw = max(abs(raw_vals[i] - raw_vals[i - 1]) for i in range(1, len(raw_vals)))
    peak_out = max(abs(out_vals[i] - out_vals[i - 1]) for i in range(1, len(out_vals)))
    assert peak_out / peak_raw < 0.15
    assert peak_out / peak_raw == pytest.approx(0.12, abs=0.03)
