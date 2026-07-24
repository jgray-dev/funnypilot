"""FunnyPilot v3.3.9 — SlaSpeedRamp invariants.

Reviewed with a second model consult before implementation: sqrt (distance,
not time) envelope, deliberately NO predictive up-ramp, dropout hold on
resolver data flicker. See sla_ramp.py's module docstring for the mechanism.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.sla_ramp import (
  SlaSpeedRamp, A_DECEL, ARRIVAL_LEAD_T, RELEASE_RATE_UP, RELEASE_RATE_DOWN, HOLD_S, CAP_INACTIVE,
)

DT = 0.05  # planner rate


class TestSlaSpeedRamp:
  def test_inactive_returns_cap_inactive(self):
    r = SlaSpeedRamp(DT)
    assert r.update(False, 20.0, 0.0, 0.0, 15.0) == CAP_INACTIVE

  def test_no_next_zone_passes_current_target_through(self):
    r = SlaSpeedRamp(DT)
    out = r.update(True, 20.0, 0.0, 0.0, 15.0)
    assert out == 20.0
    for _ in range(20):
      out = r.update(True, 20.0, 0.0, 0.0, 15.0)
    assert out == 20.0

  def test_seeds_without_a_step_on_activation(self):
    r = SlaSpeedRamp(DT)
    # first call while active: no prior _out to ramp from -> exact seed
    out = r.update(True, 12.0, 0.0, 0.0, 12.0)
    assert out == 12.0

  def test_far_from_slower_next_zone_no_effect_yet(self):
    r = SlaSpeedRamp(DT)
    r.update(True, 30.0, 20.0, 100000.0, 30.0)  # absurdly far -> envelope way above current
    out = r.update(True, 30.0, 20.0, 100000.0, 30.0)
    assert out == 30.0

  def test_engages_and_converges_to_next_target_at_boundary(self):
    r = SlaSpeedRamp(DT)
    v_ego = 30.0
    current, next_t = 30.0, 20.0
    # distance at which the envelope exactly equals current_target:
    # current^2 = next^2 + 2*A_DECEL*(d - next*LEAD) -> solve d
    d = ((current ** 2 - next_t ** 2) / (2 * A_DECEL)) + next_t * ARRIVAL_LEAD_T
    out = None
    # run only until arrival (real distance hits 0) -- staying AT the
    # boundary forever without the caller advancing to the next zone (as
    # SLA's own resolver would, at the real boundary) is not a real scenario
    for _ in range(int(d / (v_ego * DT)) + 2):
      out = r.update(True, current, next_t, d, v_ego)
      d = max(0.0, d - v_ego * DT)
    assert abs(out - next_t) < 0.5  # converged close to the target by the boundary

  def test_never_exceeds_current_target(self):
    r = SlaSpeedRamp(DT)
    d = 50.0
    for _ in range(50):
      out = r.update(True, 25.0, 15.0, d, 25.0)
      assert out <= 25.0 + 1e-9
      d = max(0.0, d - 25.0 * DT)

  def test_no_predictive_up_ramp_for_faster_next_zone(self):
    # approaching a FASTER zone must not raise the cap before the boundary
    r = SlaSpeedRamp(DT)
    out = r.update(True, 20.0, 30.0, 50.0, 20.0)
    assert out == 20.0
    for _ in range(40):
      out = r.update(True, 20.0, 30.0, 50.0, 20.0)
    assert out == 20.0  # stays at current_target the whole approach

  def test_up_release_after_boundary_is_rate_limited(self):
    r = SlaSpeedRamp(DT)
    r.update(True, 20.0, 999., 0., 20.0)  # settle at 20
    for _ in range(5):
      r.update(True, 20.0, 0., 0., 20.0)
    # boundary crossed: current_target itself steps up to 30
    out = r.update(True, 30.0, 0.0, 0.0, 20.0)
    assert out - 20.0 <= RELEASE_RATE_UP * DT + 1e-9
    assert out > 20.0

  def test_down_release_bounded_on_target_jump(self):
    r = SlaSpeedRamp(DT)
    r.update(True, 20.0, 0., 0., 20.0)
    out = r.update(True, 5.0, 0.0, 0.0, 20.0)  # current_target itself jumps down
    assert out >= 5.0
    assert 20.0 - out <= RELEASE_RATE_DOWN * DT + 1e-9

  def test_dropout_hold_survives_brief_flicker(self):
    r = SlaSpeedRamp(DT)
    v_ego = 25.0
    d = 60.0
    r.update(True, 25.0, 15.0, d, v_ego)
    out_before = r.update(True, 25.0, 15.0, d, v_ego)
    # data drops out for a few frames (resolver zeroed it)
    for _ in range(int(1.0 / DT)):
      out = r.update(True, 25.0, 0.0, 0.0, v_ego)
    # still constrained by the held next-zone data, not reset to current_target
    assert out <= out_before + 1e-6

  def test_dropout_hold_expires_after_hold_s(self):
    r = SlaSpeedRamp(DT)
    r.update(True, 25.0, 15.0, 60.0, 25.0)
    prev = None
    for i in range(int((HOLD_S + 1.0) / DT)):
      out = r.update(True, 25.0, 0.0, 0.0, 25.0)
      if i > int(HOLD_S / DT) + 2:  # well past the hold expiring
        assert prev is None or out >= prev  # releasing back up, never re-descending
      prev = out
    # give it enough frames at RELEASE_RATE_UP to fully recover, then confirm
    for _ in range(int(25.0 / RELEASE_RATE_UP / DT) + 5):
      out = r.update(True, 25.0, 0.0, 0.0, 25.0)
    assert out == 25.0

  def test_reset_on_inactive_then_reengage_seeds_clean(self):
    r = SlaSpeedRamp(DT)
    r.update(True, 25.0, 15.0, 10.0, 25.0)
    assert r.update(False, 0.0, 0.0, 0.0, 25.0) == CAP_INACTIVE
    out = r.update(True, 18.0, 0.0, 0.0, 18.0)
    assert out == 18.0  # clean reseed, no memory of the old ramp

  def test_standstill_safe_no_division_blowup(self):
    r = SlaSpeedRamp(DT)
    out = r.update(True, 20.0, 10.0, 30.0, 0.0)  # v_ego == 0
    assert 0.0 <= out <= 20.0

  def test_nonfinite_next_target_ignored(self):
    r = SlaSpeedRamp(DT)
    out = r.update(True, 20.0, float("nan"), float("inf"), 20.0)
    assert out == 20.0
