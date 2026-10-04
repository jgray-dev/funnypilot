"""Bounded comfort/yield scheduling; physical EPS limits are tested separately."""
import pytest

from openpilot.selfdrive.controls.lib.lat_handback import (
  LatHandback, PRESS_SCALE, NUDGE_SCALE, DRIVER_ZERO_ALLOWANCE, DIVERGE_LOW, DIVERGE_HIGH, T_SOFT, T_FIRM,
  INTEGRATOR_FREEZE_FRAC,
)
from openpilot.selfdrive.controls.lib.override_gate import ENGAGE_TIME, RELEASE_TIME

DT = 0.01


def run(hb, n, pressed, desired=0.0, measured=0.0):
  out = 1.0
  for _ in range(n):
    out = hb.update(pressed, desired, measured)
  return out


def press_and_release(hb, desired, measured, press_s=1.0):
  run(hb, int(press_s / DT), True, desired, measured)
  assert hb.engaged
  run(hb, int(RELEASE_TIME / DT) + 1, False, desired, measured)
  assert hb.ramping


def ramp_to_full(hb, desired, measured, limit_s=5.0):
  """Step until the ramp completes; return (frames, max per-frame scale step)."""
  frames = 0
  worst = 0.0
  prev = hb.scale
  while hb.ramping and frames < int(limit_s / DT):
    hb.update(False, desired, measured)
    worst = max(worst, hb.scale - prev)
    prev = hb.scale
    frames += 1
  return frames, worst


class TestNoInterventionIsAnExactNoOp:
  def test_scale_is_one_forever(self):
    hb = LatHandback(DT)
    for _ in range(2000):
      assert hb.update(False, 2.0, 1.0) == 1.0
    assert not hb.engaged and not hb.ramping
    assert not hb.soft_integrator


class TestComfortPressHysteresis:
  def test_dwell_hysteresis_rejects_brief_threshold_crossings(self):
    """Short threshold crossings alone do not engage comfort softening.
    Their physical cause cannot be inferred from the boolean."""
    hb = LatHandback(DT)
    for _ in range(6):
      run(hb, int(0.2 / DT), True, 2.0, 1.0)
      run(hb, int(0.2 / DT), False, 2.0, 1.0)
      assert not hb.engaged
      assert hb.scale == 1.0

  def test_sustained_press_reaches_the_floor(self):
    hb = LatHandback(DT)
    run(hb, int((ENGAGE_TIME + 1.0) / DT), True, 4.0, 1.0)
    assert hb.engaged
    assert abs(hb.scale - PRESS_SCALE) < 0.01

  def test_never_scales_above_one(self):
    hb = LatHandback(DT)
    press_and_release(hb, 2.0, 1.0)
    for _ in range(1000):
      assert hb.update(False, 2.0, 1.0) <= 1.0 + 1e-12


class TestReturnIsScheduledByDivergence:
  def test_small_gap_gets_the_normal_ramp(self):
    """MUTATION: use a fixed release time constant (the v3.2.3st behaviour).

    A small gap retains the existing slow return. This checks the schedule,
    not a physical cause of the reported corner oscillation."""
    hb = LatHandback(DT)
    press_and_release(hb, 1.0, 1.0 - DIVERGE_LOW / 2)
    assert abs(hb.ramp_duration - T_SOFT) < 1e-9
    frames, _ = ramp_to_full(hb, 1.0, 1.0)
    assert abs(frames * DT - T_SOFT) < 0.05

  def test_large_gap_gets_the_longer_ramp(self):
    """Greater disagreement must not accelerate the return toward the model."""
    hb = LatHandback(DT)
    press_and_release(hb, 3.5, 3.5 - DIVERGE_HIGH * 1.5)
    assert abs(hb.ramp_duration - T_FIRM) < 1e-9
    frames, _ = ramp_to_full(hb, 3.5, 3.5)
    assert abs(frames * DT - T_FIRM) < 0.05

  def test_duration_is_monotone_in_the_gap(self):
    prev = 0.0
    for gap in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 4.0):
      hb = LatHandback(DT)
      press_and_release(hb, gap, 0.0)
      assert hb.ramp_duration >= prev - 1e-9
      prev = hb.ramp_duration
    assert abs(prev - T_FIRM) < 1e-9

  def test_the_corner_case_return_is_far_gentler_than_before(self):
    """The concrete claim: for a corner-sized gap the per-frame authority step
    is a small fraction of what a 0.15 s first-order release delivered."""
    hb = LatHandback(DT)
    press_and_release(hb, 2.0, 1.4)  # 0.6 m/s^2 apart
    _, worst = ramp_to_full(hb, 2.0, 1.4)
    old_first_step = (1.0 - PRESS_SCALE) * (DT / 0.15)  # v3.2.3st filter, frame 1
    assert worst < old_first_step / 2

  def test_ramp_is_smooth_at_both_ends(self):
    """MUTATION: use a linear ramp. A corner in the torque schedule at either
    end of the return is exactly the kind of edge that is felt."""
    hb = LatHandback(DT)
    press_and_release(hb, 2.0, 1.4)
    steps = []
    prev = hb.scale
    while hb.ramping:
      hb.update(False, 2.0, 1.4)
      steps.append(hb.scale - prev)
      prev = hb.scale
    assert steps[0] < max(steps) / 5     # eases in
    assert steps[-1] < max(steps) / 5    # eases out
    assert all(s >= -1e-12 for s in steps)  # monotone: never gives authority back


class TestDivergenceMeasurement:
  def test_peak_is_held_across_the_press(self):
    """MUTATION: sample the gap only at the release instant. A driver may align the car just before release; a single instantaneous
    sample would erase the earlier disagreement."""
    hb = LatHandback(DT)
    run(hb, int(0.6 / DT), True, 4.0, 0.5)   # 3.5 m/s^2 apart mid-press
    run(hb, int(0.1 / DT), True, 1.0, 1.0)   # aligned right before letting go
    run(hb, int(RELEASE_TIME / DT) + 1, False, 1.0, 1.0)
    assert hb.divergence > DIVERGE_HIGH * 0.5
    assert hb.ramp_duration > T_SOFT

  def test_stale_spikes_bleed_away(self):
    """MUTATION: pure max-hold with no bleed. A single spike at the start of a
    long intervention would then schedule every handback for the rest of it."""
    hb = LatHandback(DT)
    run(hb, int(0.6 / DT), True, 4.0, 0.5)
    run(hb, int(4.0 / DT), True, 1.0, 1.0)   # long, calm remainder
    run(hb, int(RELEASE_TIME / DT) + 1, False, 1.0, 1.0)
    assert hb.divergence < DIVERGE_LOW + 1e-9
    assert abs(hb.ramp_duration - T_SOFT) < 1e-9


class TestIntegratorGating:
  def test_frozen_through_the_press_and_the_early_ramp(self):
    """MUTATION: drop soft_integrator. A frozen integrator still HOLDS the
    pre-override wind-up; dumping it back in at handback is the other half of
    the bite."""
    hb = LatHandback(DT)
    run(hb, int(1.0 / DT), True, 2.0, 1.0)
    assert hb.soft_integrator
    run(hb, int(RELEASE_TIME / DT) + 1, False, 2.0, 1.0)
    assert hb.soft_integrator
    while hb.ramping and hb.progress < INTEGRATOR_FREEZE_FRAC - 0.01:
      hb.update(False, 2.0, 1.0)
      assert hb.soft_integrator
    ramp_to_full(hb, 2.0, 1.0)
    assert not hb.soft_integrator

  def test_released_once_the_ramp_is_done(self):
    hb = LatHandback(DT)
    press_and_release(hb, 2.0, 1.0)
    ramp_to_full(hb, 2.0, 1.0)
    assert hb.scale == 1.0
    assert not hb.soft_integrator


class TestReEngagementAndReset:
  def test_a_new_press_cancels_the_ramp(self):
    """The reported cycle is grab / loosen / grab. A press landing mid-ramp
    must go straight back to the floor, not fight the ramp."""
    hb = LatHandback(DT)
    press_and_release(hb, 2.0, 1.0)
    for _ in range(10):
      hb.update(False, 2.0, 1.0)
    assert hb.ramping
    run(hb, int((ENGAGE_TIME + 0.5) / DT), True, 4.0, 1.0)
    assert not hb.ramping and hb.engaged
    assert abs(hb.scale - PRESS_SCALE) < 0.02

  def test_ramp_starts_from_the_current_scale_not_from_the_floor(self):
    """MUTATION: seed the ramp at PRESS_SCALE. Re-pressing mid-ramp and
    releasing again would then step the torque DOWN at the release edge."""
    hb = LatHandback(DT)
    press_and_release(hb, 2.0, 1.0)
    for _ in range(30):
      hb.update(False, 2.0, 1.0)
    mid = hb.scale
    run(hb, int(0.2 / DT), True, 2.0, 1.0)   # a blip that does not engage
    assert hb.scale >= mid - 1e-12

  def test_reset_returns_to_full_authority(self):
    hb = LatHandback(DT)
    run(hb, int(1.0 / DT), True, 2.0, 1.0)
    hb.reset()
    assert hb.scale == 1.0
    assert not hb.engaged and not hb.ramping and not hb.soft_integrator
    assert hb.update(False, 2.0, 1.0) == 1.0


@pytest.mark.parametrize('direction', [-1, 1])
def test_small_correction_preserves_more_assistance(direction):
  hb = LatHandback(DT)
  for _ in range(200):
    hb.update(True, direction*1.0, direction*.8, direction*155.)
  assert .82 < hb.scale <= NUDGE_SCALE
  assert hb.soft_integrator and not hb.input_fault
  # Relaxing force without releasing must not start adding torque again.
  before = hb.scale
  run(hb, 100, True, 1.0, 1.0)
  assert hb.scale <= before


@pytest.mark.parametrize('direction', [-1, 1])
def test_finite_force_never_latches_comfort_assistance_at_zero(direction):
  hb = LatHandback(DT)
  for i in range(500):
    # Driver continues guiding through a direction change. There is deliberately
    # no quiet release; the scalar must still leave useful assistance available.
    desired = 1. if i < 100 else -1.
    assert PRESS_SCALE <= hb.update(True, desired, desired*.9, direction*400.) <= 1.0
    assert not hb.input_fault
  assert hb.scale == pytest.approx(PRESS_SCALE)


def test_short_nudge_stops_return_immediately():
  hb = LatHandback(DT)
  press_and_release(hb, 2.0, 1.0)
  run(hb, 30, False, 2.0, 1.0)
  before = hb.scale
  for _ in range(20):
    assert hb.update(True, 2.0, 1.0) == before
  hb.update(False, 2.0, 1.0)
  assert 0.0 <= hb.scale-before < .001


@pytest.mark.parametrize('field', range(3))
@pytest.mark.parametrize('bad', [float('nan'), float('inf')])
def test_invalid_inputs_yield_without_nan_output(field, bad):
  hb = LatHandback(DT)
  values = [1., 1., 0.]
  values[field] = bad
  assert hb.update(False, *values) == 0.0
  assert hb.soft_integrator


@pytest.mark.parametrize('direction', [-1, 1])
def test_force_aiding_the_current_torque_does_not_trigger_full_yield(direction):
  hb = LatHandback(DT)
  hb.update(True, 1.0, 1.0, direction*DRIVER_ZERO_ALLOWANCE)
  assert not hb.input_fault and hb.scale == 1.0
