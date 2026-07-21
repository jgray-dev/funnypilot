"""FunnyPilot v3.3.8 — BumpDamper invariants.

ACTED-ON hypothesis (see bump_damper.py docstring): unit-tests the timing
contract and safety bounds. Whether the mechanism actually cures the
railroad-track oscillation is verified on-road via the existing BUMP/EPS
dev-UI readouts, not here.
"""
import math

from openpilot.selfdrive.controls.lib.bump_damper import BumpDamper, TRIGGER_DEG_S, MIN_DAMP, HOLD_S, RECOVER_S

DT = 0.01


class TestBumpDamper:
  def test_noop_below_trigger(self):
    d = BumpDamper(DT)
    for _ in range(50):
      assert d.update(TRIGGER_DEG_S - 0.1) == 1.0
    assert not d.active

  def test_collapses_instantly_on_trigger(self):
    d = BumpDamper(DT)
    d.update(0.0)
    scale = d.update(TRIGGER_DEG_S + 2.0)
    assert scale == MIN_DAMP
    assert d.active

  def test_negative_pitch_also_triggers(self):
    # bumps produce both dip and rebound edges
    d = BumpDamper(DT)
    scale = d.update(-(TRIGGER_DEG_S + 2.0))
    assert scale == MIN_DAMP

  def test_holds_at_floor_for_hold_duration(self):
    d = BumpDamper(DT)
    d.update(TRIGGER_DEG_S + 2.0)
    n_hold_frames = int(HOLD_S / DT)
    for _ in range(n_hold_frames - 1):
      scale = d.update(0.0)  # signal drops back to baseline immediately
      assert scale == MIN_DAMP

  def test_recovers_linearly_after_hold(self):
    d = BumpDamper(DT)
    d.update(TRIGGER_DEG_S + 2.0)
    n_hold_frames = int(HOLD_S / DT)
    for _ in range(n_hold_frames):
      d.update(0.0)
    prev = d.scale
    for _ in range(10):
      scale = d.update(0.0)
      assert scale >= prev  # monotone recovery
      prev = scale
    assert scale > MIN_DAMP

  def test_fully_recovers_and_becomes_noop_again(self):
    d = BumpDamper(DT)
    d.update(TRIGGER_DEG_S + 2.0)
    total_frames = int((HOLD_S + RECOVER_S) / DT) + 5
    for _ in range(total_frames):
      d.update(0.0)
    assert d.scale == 1.0
    assert not d.active

  def test_retrigger_extends_hold(self):
    d = BumpDamper(DT)
    d.update(TRIGGER_DEG_S + 2.0)
    half_hold = int(HOLD_S / DT / 2)
    for _ in range(half_hold):
      d.update(0.0)
    d.update(TRIGGER_DEG_S + 2.0)  # second bump mid-hold
    assert d.scale == MIN_DAMP
    # still within a fresh full hold window from the retrigger
    for _ in range(int(HOLD_S / DT) - 1):
      assert d.update(0.0) == MIN_DAMP

  def test_scale_always_in_bounds(self):
    d = BumpDamper(DT)
    values = [0.0, 10.0, -10.0, 3.0, 7.0, 0.0] * 50
    for v in values:
      s = d.update(v)
      assert MIN_DAMP <= s <= 1.0

  def test_nan_and_inf_safe(self):
    d = BumpDamper(DT)
    assert d.update(float("nan")) == 1.0
    d2 = BumpDamper(DT)
    d2.update(float("inf"))  # treated as non-finite -> no trigger
    assert d2.scale == 1.0

  def test_reset_clears_state(self):
    d = BumpDamper(DT)
    d.update(TRIGGER_DEG_S + 2.0)
    assert d.active
    d.reset()
    assert d.scale == 1.0
    assert not d.active

  def test_active_property_matches_scale(self):
    d = BumpDamper(DT)
    assert not d.active
    d.update(TRIGGER_DEG_S + 2.0)
    assert d.active
