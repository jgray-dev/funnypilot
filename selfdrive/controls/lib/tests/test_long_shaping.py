"""FunnyPilot v3.2.6e — invariants of the longitudinal output shaping.

Import-light (numpy only), so it runs without the full openpilot environment:
  python3 -m pytest selfdrive/controls/lib/tests/test_long_shaping.py
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../.."))

from openpilot.selfdrive.controls.lib import long_shaping

from openpilot.selfdrive.controls.lib.long_shaping import (
  AccelJerkShaper, LeadGrace,
  JERK_DOWN_BP, JERK_DOWN_V,
  LEAD_GRACE_ARM_TIME, LEAD_GRACE_HOLD_TIME, LEAD_GRACE_RELEASE_TIME,
)

DT = 0.05
EPS = 1e-9


class TestAccelJerkShaper:
  def test_up_jerk_limited(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    out = s.update(2.0, jerk_up=1.8)
    assert abs(out - 1.8 * DT) < EPS

  def test_up_converges_without_overshoot(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    prev = 0.0
    for _ in range(100):
      out = s.update(1.0, jerk_up=1.8)
      assert prev - EPS <= out <= 1.0 + EPS
      prev = out
    assert abs(out - 1.0) < EPS

  def test_mild_braking_comfort_limited(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    out = s.update(-1.0)
    # mild demand: bounded by the comfort down-jerk (4 m/s^3)
    assert abs(out - (-JERK_DOWN_V[1] * DT)) < EPS

  def test_strong_braking_barely_delayed(self):
    # SAFETY INVARIANT: a -3.5 m/s^2 demand must be reached in well under
    # half a second. At the max down-jerk of 12 m/s^3 that is <= 6 frames.
    s = AccelJerkShaper(DT, a_init=0.0)
    frames = 0
    while s.update(-3.5) > -3.5 + EPS:
      frames += 1
      assert frames < 10, "strong braking demand delayed too long"
    assert frames <= int(3.5 / (JERK_DOWN_V[0] * DT)) + 1

  def test_down_jerk_scales_with_demand(self):
    mild = AccelJerkShaper(DT, a_init=0.0).update(JERK_DOWN_BP[1])
    strong = AccelJerkShaper(DT, a_init=0.0).update(JERK_DOWN_BP[0])
    assert strong < mild < 0.0

  def test_jerk_down_is_monotone(self):
    """SAFETY INVARIANT, v3.5.3. The table must never allow a FIRMER braking
    demand a GENTLER slew than a milder one. A single mis-ordered value would
    make hard braking softer than light braking, which is the one thing this
    module promises it cannot do — and it would look like a harmless tuning
    edit in review."""
    assert JERK_DOWN_BP == sorted(JERK_DOWN_BP), "breakpoints must ascend for np.interp"
    assert JERK_DOWN_V == sorted(JERK_DOWN_V, reverse=True), \
      "more negative demand must never get a lower jerk allowance"

  def test_lifting_off_throttle_is_gentler_than_braking(self):
    """v3.5.3. `np.interp` CLAMPS, so the old two-point table gave every target
    above -1.0 the same 4 m/s^3 — a simple throttle lift got brake-apply slew
    and dropped from full throttle to zero in a quarter second."""
    lift = AccelJerkShaper(DT, a_init=1.0).update(0.8)
    brake = AccelJerkShaper(DT, a_init=1.0).update(-1.0)
    assert (1.0 - lift) < (1.0 - brake), "a mild lift must move less per frame than a brake apply"

  def test_a_hard_demand_is_unaffected_by_the_new_breakpoints(self):
    """The interpolation variable is the DEMAND, not the current output — so
    extending the table upward cannot slow a brake application, whatever the
    shaper was doing on the previous frame."""
    from_throttle = AccelJerkShaper(DT, a_init=1.0)
    assert abs(from_throttle.update(-3.5) - (1.0 - JERK_DOWN_V[0] * DT)) < EPS

  def test_fcw_bypass_is_immediate(self):
    s = AccelJerkShaper(DT, a_init=1.0)
    out = s.update(-4.0, bypass=True)
    assert out == -4.0
    # and the shaper re-seeds from the bypassed value
    assert s.update(-4.0) == -4.0

  def test_brake_release_is_gentle(self):
    s = AccelJerkShaper(DT, a_init=-2.0)
    out = s.update(0.0, jerk_up=1.8)
    assert abs(out - (-2.0 + 1.8 * DT)) < EPS

  def test_reset(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    s.update(1.0)
    s.reset(-0.5)
    assert s.a == -0.5

  def test_nan_target_contained(self):
    s = AccelJerkShaper(DT, a_init=0.5)
    out = s.update(float("nan"))
    assert out == 0.0


def run_grace(g, seconds, **kw):
  out = None
  for _ in range(int(round(seconds / DT))):
    out = g.update(**kw)
  return out


class TestLeadGrace:
  def test_no_lead_never_tracked_passthrough(self):
    g = LeadGrace(DT)
    assert g.update(False, False, 0.0, 20.0, 30.0) == 30.0

  def test_flicker_caps_at_lead_speed(self):
    g = LeadGrace(DT)
    # follow a 15 m/s lead for 2 s, then it flickers off
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=15.0, v_ego=15.0, v_cruise=30.0)
    out = g.update(False, False, 0.0, 15.0, 30.0)
    assert abs(out - 15.0) < EPS

  def test_cap_never_below_v_ego(self):
    # ROBUSTNESS INVARIANT: grace may hold the car back, never brake it.
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=10.0, v_ego=10.0, v_cruise=30.0)
    # ego is now faster than the lead was (e.g. lead data was stale)
    out = g.update(False, False, 0.0, 18.0, 30.0)
    assert out >= 18.0 - EPS

  def test_short_lived_lead_not_armed(self):
    g = LeadGrace(DT)
    # lead only seen for 0.5 s (< ARM time): its loss has no authority
    run_grace(g, min(0.5, LEAD_GRACE_ARM_TIME / 2), lead_status=True, following=True, v_lead=5.0, v_ego=20.0, v_cruise=30.0)
    assert g.update(False, False, 0.0, 20.0, 30.0) == 30.0

  def test_not_following_not_armed(self):
    g = LeadGrace(DT)
    # lead visible but not the MPC's constraint (far ahead)
    run_grace(g, 3.0, lead_status=True, following=False, v_lead=5.0, v_ego=20.0, v_cruise=30.0)
    assert g.update(False, False, 0.0, 20.0, 30.0) == 30.0

  def test_release_ramp_and_full_release(self):
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=15.0, v_ego=15.0, v_cruise=30.0)
    # hold phase
    out_hold = run_grace(g, LEAD_GRACE_HOLD_TIME, lead_status=False, following=False, v_lead=0.0, v_ego=15.0, v_cruise=30.0)
    assert abs(out_hold - 15.0) < 0.5
    # mid-ramp: strictly between hold speed and cruise
    out_mid = run_grace(g, LEAD_GRACE_RELEASE_TIME / 2, lead_status=False, following=False, v_lead=0.0, v_ego=15.0, v_cruise=30.0)
    assert 15.0 < out_mid < 30.0
    # fully released
    out_end = run_grace(g, LEAD_GRACE_RELEASE_TIME, lead_status=False, following=False, v_lead=0.0, v_ego=15.0, v_cruise=30.0)
    assert out_end == 30.0

  def test_cap_respects_lower_cruise(self):
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=25.0, v_ego=20.0, v_cruise=30.0)
    # user set speed below the cap: cruise wins
    assert g.update(False, False, 0.0, 20.0, 22.0) == 22.0

  def test_reacquire_resets(self):
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=15.0, v_ego=15.0, v_cruise=30.0)
    g.update(False, False, 0.0, 15.0, 30.0)
    # new lead appears: loss timer resets, tracking restarts
    g.update(True, True, 12.0, 15.0, 30.0)
    assert g._lost_t == 0.0


class TestAccelClipResetOnDisengage:
  """FunnyPilot v3.5.3 — `prev_accel_clip` MUST be reset with the rest of the
  planner state.

  It feeds a +/-0.05-per-frame rate limiter on the accel CEILING. That limiter
  is there to stop the ceiling stepping WHILE ENGAGED. Across a disengagement
  there is no continuity worth preserving, and leaving the stale value behind
  means the ceiling walks back up at 1.0 m/s^2 per second on re-engage:
  disengage mid-corner (turn limiting has pulled it to ~0.1) or during an SLA
  gas gate (which pins it to coast accel, NEGATIVE on a downhill), then
  re-engage on a straight, and the car will not accelerate for one to two
  seconds. Same input, different response depending on invisible history.

  ASSERTED ON THE AST, because `longitudinal_planner` imports the acados MPC
  and cannot be constructed off-device. Same technique as the single-writer
  guard in test_cruise_ext_sla_ramp.py and the no-syscalls guard in
  test_scc_learn.py: when the runtime is unreachable, the structure is what is
  left to pin.
  """

  @staticmethod
  def _reset_block():
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / 'longitudinal_planner.py').read_text()
    for node in ast.walk(ast.parse(src)):
      # the `if reset_state:` branch inside LongitudinalPlanner.update
      if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
          and node.test.id == 'reset_state'):
        return node
    return None

  def test_the_reset_branch_exists(self):
    """Anti-vacuous: if the branch is ever renamed, every assertion below would
    pass on an empty search rather than fail loudly."""
    assert self._reset_block() is not None, "could not find the `if reset_state:` branch"

  def test_prev_accel_clip_is_reset_with_the_rest_of_the_state(self):
    import ast
    block = self._reset_block()
    assigned = {
      ast.unparse(t) for stmt in ast.walk(block)
      if isinstance(stmt, ast.Assign) for t in stmt.targets
    }
    calls = {
      ast.unparse(n.func) for n in ast.walk(block) if isinstance(n, ast.Call)
    }
    # the state this branch has always reset, as a sanity anchor
    assert 'self.a_desired' in assigned
    assert 'self.shaper.reset' in calls
    # ...and the one v3.5.3 added
    assert 'self.prev_accel_clip' in assigned, (
      "prev_accel_clip survives a disengagement and throttles the accel ceiling " +
      "for ~1-2 s on re-engage; it must be reset here"
    )


class TestTheStopGovernor:
  """FunnyPilot v3.6.6 — "in stop and go city traffic it frequently decides it
  doesn't need to brake early for a stopped car, and is very much willing to
  rear end someone if I don't take over braking."

  The MPC's following constraint is a COST, not a limit, and in blended mode it
  competes with a model velocity plan weighted 50x higher. This is the fork's
  own idiom applied to the one case where that trade-off is dangerous: a
  speed cap from stopping GEOMETRY, which every MPC mode honours through
  v_cruise.
  """
  DT = 0.05

  def _gov(self):
    return long_shaping.StopGovernor(self.DT)

  def _settle(self, g, d_rel, v_lead, v_ego, v_cruise, n=200):
    out = v_cruise
    for _ in range(n):
      out = g.update(True, d_rel, v_lead, v_ego, v_cruise)
    return out

  def test_a_stopped_car_close_ahead_caps_hard(self):
    """MUTATION: delete the update() call from the planner, or return v_cruise
    unconditionally. 60 m from a stationary car at 25 m/s is not a speed you
    can still stop comfortably from."""
    g = self._gov()
    out = self._settle(g, 60.0, 0.0, 25.0, 30.0)
    assert out == pytest.approx(g.raw_cap(60.0, 0.0), abs=0.01)
    assert out < 15.0

  def test_the_envelope_is_the_stopping_geometry(self):
    """sqrt(v_lead^2 + 2*a*(d - gap)) — no fudge factor. At 60 m from a stopped
    car with a 9 m gap and 1.8 m/s^2 that is exactly 13.5 m/s."""
    g = self._gov()
    assert g.raw_cap(60.0, 0.0) == pytest.approx(math.sqrt(2 * 1.8 * 51.0), abs=0.01)
    assert g.raw_cap(9.0, 0.0) == pytest.approx(0.0)

  def test_it_loosens_monotonically_with_distance(self):
    g = self._gov()
    caps = [g.raw_cap(d, 0.0) for d in (20.0, 40.0, 80.0, 160.0)]
    assert caps == sorted(caps)
    assert all(b > a for a, b in zip(caps, caps[1:], strict=False))

  def test_it_can_only_ever_slow_the_car(self):
    """THE INVARIANT. This is a speed-domain governor and the MPC still owns the
    stop itself; it must never be able to raise a target. MUTATION: drop either
    `min(..., v_cruise)`."""
    g = self._gov()
    for d in (5.0, 50.0, 500.0, 5000.0):
      assert self._settle(g, d, 0.0, 10.0, 20.0, n=400) <= 20.0 + 1e-9
      g.reset()

  def test_a_moving_lead_is_the_mpcs_problem(self):
    """MUTATION: drop the STOP_GOV_LEAD_V gate. A general follow-distance
    governor competing with the solver is exactly the stacked-authority mistake
    v3.2.6e removed."""
    g = self._gov()
    assert self._settle(g, 40.0, 20.0, 25.0, 30.0) == pytest.approx(30.0)
    assert g.cap is None

  def test_the_authority_fades_as_the_lead_speeds_up(self):
    """Not a switch: a lead accelerating away past the threshold must not step
    the cap off. MUTATION: make the gate a hard boundary."""
    g = self._gov()
    caps = []
    for vl in (0.0, 3.0, 5.0, 5.8):
      g.reset()
      caps.append(self._settle(g, 45.0, vl, 20.0, 28.0))
    assert caps == sorted(caps)
    assert caps[-1] > caps[0]
    assert caps[-1] < 28.0, "it must still have some say at the top of the band"

  def test_a_one_frame_ghost_cannot_grab_the_cruise_target(self):
    """STOP_GOV_CONFIRM_N. A radar ghost at 30 m would otherwise drop the target
    for a frame. MUTATION: act on the first frame."""
    g = self._gov()
    assert g.update(True, 30.0, 0.0, 25.0, 30.0) == pytest.approx(30.0)
    assert g.update(False, 0.0, 0.0, 25.0, 30.0) == pytest.approx(30.0)

  def test_the_cap_is_seeded_at_the_current_speed(self):
    """No step into the governor's min(). MUTATION: seed at the raw envelope
    and engagement becomes a cliff in v_cruise."""
    g = self._gov()
    for _ in range(long_shaping.STOP_GOV_CONFIRM_N):
      out = g.update(True, 40.0, 0.0, 22.0, 28.0)
    assert out == pytest.approx(22.0, abs=0.01)

  def test_it_falls_at_a_bounded_rate(self):
    """MUTATION: drop the rate limit. A flickering track would yank the cruise
    target; this is an early shave and the MPC's lead constraint is the real
    emergency brake, so it does not need to arrive instantly."""
    g = self._gov()
    for _ in range(long_shaping.STOP_GOV_CONFIRM_N):
      g.update(True, 40.0, 0.0, 25.0, 28.0)
    before = g.cap
    g.update(True, 40.0, 0.0, 25.0, 28.0)
    assert before - g.cap <= long_shaping.STOP_GOV_FALL_RATE * self.DT + 1e-9

  def test_losing_the_lead_releases_it(self):
    g = self._gov()
    self._settle(g, 40.0, 0.0, 20.0, 28.0)
    assert g.update(False, 0.0, 0.0, 20.0, 28.0) == pytest.approx(28.0)
    assert g.cap is None

  def test_garbage_never_constrains(self):
    g = self._gov()
    for d, vl in ((float('nan'), 0.0), (40.0, float('nan')), (float('inf'), 0.0)):
      g.reset()
      out = self._settle(g, d, vl, 20.0, 28.0, n=10)
      assert out <= 28.0 + 1e-9 and out == out
