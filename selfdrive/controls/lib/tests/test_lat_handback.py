"""FunnyPilot v3.4.9 — LatHandback invariants (import-light, stdlib only).

The reported defect is a LIMIT CYCLE through a corner: grab, loosen, the model
takes control and bites, grab again. v3.2.8's OverrideGate stopped the
softening from chattering; nothing scheduled its RETURN, so handback was the
same near-step regardless of how far apart the two desires were.

Each test names the mutation it guards.
"""
from openpilot.selfdrive.controls.lib.lat_handback import (
  LatHandback, PRESS_SCALE, DIVERGE_LOW, DIVERGE_HIGH, T_SOFT, T_FIRM,
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


class TestPressBehaviourUnchanged:
  def test_dwell_hysteresis_still_rejects_inertia_blips(self):
    """MUTATION: drop OverrideGate. A hard self-steer bite can latch
    steeringPressed for ~0.1-0.25 s with no driver involved; that must never
    engage the softening (the v3.2.8 bite-then-loosen limit cycle)."""
    hb = LatHandback(DT)
    for _ in range(6):
      run(hb, int(0.2 / DT), True, 2.0, 1.0)
      run(hb, int(0.2 / DT), False, 2.0, 1.0)
      assert not hb.engaged
      assert hb.scale == 1.0

  def test_sustained_press_reaches_the_floor(self):
    hb = LatHandback(DT)
    run(hb, int((ENGAGE_TIME + 1.0) / DT), True, 3.0, 1.0)
    assert hb.engaged
    assert abs(hb.scale - PRESS_SCALE) < 0.01

  def test_never_scales_above_one(self):
    hb = LatHandback(DT)
    press_and_release(hb, 2.0, 1.0)
    for _ in range(1000):
      assert hb.update(False, 2.0, 1.0) <= 1.0 + 1e-12


class TestReturnIsScheduledByDivergence:
  def test_small_gap_gets_the_long_ramp(self):
    """MUTATION: use a fixed release time constant (the v3.2.3st behaviour).

    This is the reported corner: the driver has settled the car on the line
    they want, so the two desires are close and there is nothing to correct
    urgently. Snatching authority back here is what bites."""
    hb = LatHandback(DT)
    press_and_release(hb, 1.0, 1.0 - DIVERGE_LOW / 2)
    assert abs(hb.ramp_duration - T_SOFT) < 1e-9
    frames, _ = ramp_to_full(hb, 1.0, 1.0)
    assert abs(frames * DT - T_SOFT) < 0.05

  def test_large_gap_gets_the_short_ramp(self):
    """The evasive case: the car is far off the model's path and dawdling
    there is the wrong trade."""
    hb = LatHandback(DT)
    press_and_release(hb, 3.5, 3.5 - DIVERGE_HIGH * 1.5)
    assert abs(hb.ramp_duration - T_FIRM) < 1e-9
    frames, _ = ramp_to_full(hb, 3.5, 3.5)
    assert abs(frames * DT - T_FIRM) < 0.05

  def test_duration_is_monotone_in_the_gap(self):
    prev = 1e9
    for gap in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 4.0):
      hb = LatHandback(DT)
      press_and_release(hb, gap, 0.0)
      assert hb.ramp_duration <= prev + 1e-9
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
    """MUTATION: sample the gap only at the release instant. The driver in the
    reported scenario has ALIGNED the car by the time they relax, so an
    instantaneous sample would read ~0 and would schedule the wrong ramp for a
    genuinely evasive intervention."""
    hb = LatHandback(DT)
    run(hb, int(0.6 / DT), True, 4.0, 0.5)   # 3.5 m/s^2 apart mid-press
    run(hb, int(0.1 / DT), True, 1.0, 1.0)   # aligned right before letting go
    run(hb, int(RELEASE_TIME / DT) + 1, False, 1.0, 1.0)
    assert hb.divergence > DIVERGE_HIGH * 0.5
    assert hb.ramp_duration < T_SOFT

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
    run(hb, int((ENGAGE_TIME + 0.5) / DT), True, 2.0, 1.0)
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
