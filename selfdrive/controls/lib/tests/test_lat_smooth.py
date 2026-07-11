"""FunnyPilot v3.2.10 — LatSmoother invariants (import-light, stdlib only).

The contract: the validated 3.1.0e delta/5 schedule, exactly, plus
continuity at any cadence — and nothing else.
"""
import math

from openpilot.selfdrive.controls.lib.lat_smooth import LatSmoother, T_MODEL, PHASE_LEAD, HEALTH_FULL

DT = 0.01  # 100 Hz


class TestLatSmoother:
  def test_validated_delta5_schedule(self):
    # at a healthy 100 Hz, the five frames after a knot emit
    # prev + (0.2, 0.4, 0.6, 0.8, 1.0) * delta — bit-compatible with 3.1.0e+
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)  # establish the trajectory at 0
    outs = []
    s.update(1.0, True, 0.05)  # new knot: delta = 1.0
    outs.append(s.out)
    for i in range(1, 5):
      outs.append(s.update(1.0, False, 0.05 + i * DT))
    expected = [0.2, 0.4, 0.6, 0.8, 1.0]
    for got, want in zip(outs, expected, strict=True):
      assert abs(got - want) < 1e-9

  def test_moves_every_frame(self):
    # the whole point: no frame between knots is ever flat
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    prev = s.out
    for i in range(1, 5):
      out = s.update(1.0, False, 0.05 + i * DT)
      assert out - prev > 0.15  # ~delta/5 per frame
      prev = out

  def test_output_always_within_knot_bracket(self):
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.5, True, 0.0)
    for i in range(1, 20):
      out = s.update(0.5, False, i * DT)
      assert 0.0 - 1e-12 <= out <= 0.5 + 1e-12

  def test_continuity_on_early_knot(self):
    # a knot arriving after only 2 control frames must not step the output:
    # prev becomes the last OUTPUT, not the stale old knot
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    s.update(1.0, False, 0.06)  # out = 0.4
    before = s.out
    s.update(2.0, True, 0.07)   # early knot; first emit = before + 0.2*(2.0-before)
    step = s.out - before
    assert abs(step - PHASE_LEAD * (2.0 - before)) < 1e-9

  def test_late_model_frame_holds_at_cur(self):
    # model stalls: alpha saturates at 1.0 and the output holds the knot value
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    for i in range(1, 30):  # 300 ms, no new knot
      out = s.update(1.0, False, 0.05 + i * DT)
    assert out == 1.0

  def test_cadence_independent_of_control_rate(self):
    # a 50 Hz control loop lands on the same time-anchored line
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    s.update(1.0, True, 0.05)
    out = s.update(1.0, False, 0.05 + 0.02)  # 20 ms later
    assert abs(out - (PHASE_LEAD + 0.02 / T_MODEL) * 1.0) < 1e-9

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
    # first active frame passes the model action through (no knot yet)
    out = s.update(5e-4, False, 1.0)
    assert out == 5e-4

  def test_health_counts_realized_frames(self):
    s = LatSmoother()
    s.reset(0.0)
    s.update(0.0, True, 0.0)
    for i in range(1, 5):
      s.update(0.0, False, i * DT)
    s.update(1.0, True, 0.05)
    assert s.health_frames == HEALTH_FULL
