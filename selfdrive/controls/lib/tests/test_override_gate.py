"""FunnyPilot v3.2.8 — OverrideGate tests (import-light, stdlib only).

The gate must make the bite-then-loosen limit cycle impossible: inertia-blip
steeringPressed patterns (short alternating press/release) never engage the
softening, while a genuine sustained takeover engages and stays engaged.
"""
from openpilot.selfdrive.controls.lib.override_gate import OverrideGate, ENGAGE_TIME, RELEASE_TIME

DT = 0.01  # 100 Hz


def run(gate, pressed, seconds):
  out = None
  for _ in range(int(round(seconds / DT))):
    out = gate.update(pressed)
  return out


class TestOverrideGate:
  def test_starts_disengaged(self):
    g = OverrideGate(DT)
    assert not g.update(False)

  def test_limit_cycle_pattern_never_engages(self):
    # the oscillation signature: ~0.15 s pressed / ~0.15 s released, repeating.
    # HKG's 5-frame debounce means real inertia blips look exactly like this.
    g = OverrideGate(DT)
    for _ in range(50):  # 15 seconds of cycling
      assert run(g, True, 0.15) is False
      assert run(g, False, 0.15) is False

  def test_blips_just_under_engage_time_never_engage(self):
    g = OverrideGate(DT)
    for _ in range(20):
      assert run(g, True, ENGAGE_TIME - DT) is False
      run(g, False, DT)  # single released frame resets the dwell

  def test_sustained_press_engages(self):
    g = OverrideGate(DT)
    assert run(g, True, ENGAGE_TIME - 2 * DT) is False
    assert run(g, True, 3 * DT) is True

  def test_engaged_survives_brief_release(self):
    # a real takeover shouldn't flicker at the torque threshold
    g = OverrideGate(DT)
    run(g, True, ENGAGE_TIME + 0.1)
    assert g.engaged
    assert run(g, False, RELEASE_TIME - 2 * DT) is True
    assert run(g, True, 0.1) is True

  def test_sustained_release_disengages(self):
    g = OverrideGate(DT)
    run(g, True, ENGAGE_TIME + 0.1)
    assert run(g, False, RELEASE_TIME + 2 * DT) is False

  def test_no_frame_to_frame_alternation_possible(self):
    # engaged state may change at most twice over a rapid random-ish toggle burst
    g = OverrideGate(DT)
    run(g, True, ENGAGE_TIME + 0.1)  # engaged
    changes = 0
    prev = g.engaged
    for i in range(200):  # 2 s of per-frame toggling
      out = g.update(i % 2 == 0)
      changes += int(out != prev)
      prev = out
    assert changes <= 1  # per-frame toggling can at most settle once, never cycle

  def test_reset(self):
    g = OverrideGate(DT)
    run(g, True, ENGAGE_TIME + 0.1)
    g.reset()
    assert not g.engaged
    assert run(g, True, ENGAGE_TIME - 2 * DT) is False
