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
from openpilot.selfdrive.controls.lib import long_shaping as ls

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


class TestTheStopGovernorOnlyActsWhileClosing:
  """FunnyPilot v3.6.7 — the launch bug v3.6.6 shipped, and it was mine.

  The envelope evaluates to exactly `v_lead` at `d == STOP_GOV_GAP_M`, so
  sitting 6 m behind a car in traffic capped the cruise target at the lead's own
  speed: the car could match it and never regain a normal gap. Reported as "the
  lead car drives from a stop and our vehicle doesn't accelerate appropriately".

  The fix is to say what this governor is FOR. It answers "am I going too fast
  for what is in front of me", which is only a question while the gap is
  shrinking.
  """
  DT = 0.05

  def _gov(self):
    return long_shaping.StopGovernor(self.DT)

  def _settle(self, g, d_rel, v_lead, v_ego, v_cruise, n=200):
    out = v_cruise
    for _ in range(n):
      out = g.update(True, d_rel, v_lead, v_ego, v_cruise)
    return out

  def test_launching_behind_a_lead_is_not_capped(self):
    """THE REPORTED CASE. Stopped 6 m back, the lead pulls away at 1 m/s and we
    are still at 0.5 — not closing, so the governor has nothing to say and the
    MPC gets the full cruise target to accelerate against. MUTATION: drop the
    `closing` term and the cap returns to exactly the lead's speed."""
    g = self._gov()
    assert self._settle(g, 6.0, 1.0, 0.5, 25.0) == pytest.approx(25.0)
    assert g.cap is None

  def test_matching_the_leads_speed_releases_it(self):
    """Once we have slowed to the lead there is nothing left to shave, and
    holding a cap at the lead's own speed is what stopped the gap ever
    recovering."""
    g = self._gov()
    self._settle(g, 40.0, 2.0, 20.0, 25.0)       # closing hard: engaged
    assert g.cap is not None
    assert self._settle(g, 40.0, 2.0, 2.0, 25.0) == pytest.approx(25.0)
    assert g.cap is None

  def test_it_still_engages_when_we_are_actually_closing(self):
    """Anti-vacuous: the whole feature must survive its own guard."""
    g = self._gov()
    out = self._settle(g, 60.0, 0.0, 25.0, 30.0)
    assert out == pytest.approx(g.raw_cap(60.0, 0.0), abs=0.01)
    assert out < 15.0

  def test_the_engage_threshold_has_hysteresis(self):
    """A car tracking a lead at a near-identical speed must not chatter the
    governor on and off. MUTATION: use one threshold for both."""
    assert long_shaping.STOP_GOV_CLOSE_OFF < long_shaping.STOP_GOV_CLOSE_ON
    g = self._gov()
    # not closing enough to engage
    assert self._settle(g, 40.0, 2.0, 2.5, 25.0, n=10) == pytest.approx(25.0)
    # engaged, then a small drop in overspeed does NOT release it
    self._settle(g, 40.0, 2.0, 20.0, 25.0)
    assert g.update(True, 40.0, 2.0, 2.5, 25.0) < 25.0

  def test_creeping_traffic_is_left_to_the_mpc(self):
    """Following at the same speed in a queue is the ordinary following problem
    and belongs to the solver, exactly as it does for a fast lead."""
    g = self._gov()
    assert self._settle(g, 8.0, 3.0, 3.0, 25.0) == pytest.approx(25.0)


class TestWhoIsDecidingTheLongitudinal:
  """FunnyPilot v3.6.7 — `long_source_code`, the dev panel's SRC readout.

  IT IS A CLASSIFIER, NOT A REPORT, and the whole content is the ORDER. Five
  things can be the binding constraint and more than one is usually "active" at
  once; getting the priority wrong produces a readout that is plausible on
  every frame and wrong on the ones that matter.
  """

  def test_the_lead_outranks_every_cap(self):
    # A speed governor lowering v_cruise while the MPC's lead obstacle is the
    # nearest one has not changed what the car is doing — the lead was already
    # holding us under that cap. Reporting SCCM there would send someone
    # looking at the corner code for a following problem.
    for gov in ("cruise", "sccVision", "sccMap", "speedLimitAssist"):
      assert ls.long_source_code("lead0", gov, 12.0, 20.0) == ls.SRC_LEAD
      assert ls.long_source_code("lead1", gov, None, 20.0) == ls.SRC_LEAD

  def test_the_stop_governor_is_credited_only_when_its_cap_is_the_one_in_force(self):
    # Engaged and binding.
    assert ls.long_source_code("cruise", "cruise", 14.0, 14.0) == ls.SRC_STOP
    # Engaged but something else took v_cruise lower: not its constraint.
    assert ls.long_source_code("cruise", "sccMap", 20.0, 14.0) == ls.SRC_SCC_M
    # Not engaged at all.
    assert ls.long_source_code("cruise", "cruise", None, 14.0) == ls.SRC_CRUISE

  def test_each_governor_reports_itself(self):
    assert ls.long_source_code("cruise", "sccVision", None, 20.0) == ls.SRC_SCC_V
    assert ls.long_source_code("cruise", "sccMap", None, 20.0) == ls.SRC_SCC_M
    assert ls.long_source_code("cruise", "speedLimitAssist", None, 20.0) == ls.SRC_SLA

  def test_the_model_plan_reports_as_e2e_and_only_when_nothing_caps(self):
    assert ls.long_source_code("e2e", "cruise", None, 20.0) == ls.SRC_E2E
    # A governor winning the min() is the more specific answer.
    assert ls.long_source_code("e2e", "sccMap", None, 20.0) == ls.SRC_SCC_M

  def test_holding_the_set_speed_is_cruise(self):
    assert ls.long_source_code("cruise", "cruise", None, 20.0) == ls.SRC_CRUISE

  def test_it_never_raises_on_junk(self):
    # It runs inside the publish path of a 20 Hz control process, so a bad
    # input must degrade to a reading rather than take plannerd down.
    for bad in (None, "", "wat", 3, object()):
      assert ls.long_source_code(bad, bad, bad, bad) in ls.SRC_NAMES

  def test_every_code_has_a_name(self):
    # The UI looks the code up in SRC_NAMES; a code with no entry draws "?".
    codes = {v for k, v in vars(ls).items() if k.startswith("SRC_") and isinstance(v, int)}
    assert codes == set(ls.SRC_NAMES)


class TestALeadCommittedToStopping:
  """FunnyPilot v3.6.8 — the stop governor acts on a lead that is BRAKING, not
  only on one that is already crawling.

  THE PROPERTY THAT MAKES ARMING EARLIER SAFE IS THE ENVELOPE, NOT A THRESHOLD.
  Where the lead is provably stopping, the geometry is measured to its stop
  POINT; a lead easing off gently projects that point far away and the cap
  comes out above any legal speed, so nothing happens. These tests pin that
  self-limiting behaviour, because without it this is the stacked-authority
  mistake v3.2.6e removed.
  """

  def _gov(self):
    return ls.StopGovernor(0.05)

  def _settle(self, gov, d, vl, ve, vc, a, n=8):
    out = vc
    for _ in range(n):
      out = gov.update(True, d, vl, ve, vc, a)
    return out

  def test_a_gentle_lift_never_arms_it(self):
    # -0.5 m/s^2 is coasting, not braking. Below DECEL_MIN, so not committed,
    # and at 25 m/s the lead is far above STOP_GOV_LEAD_V too.
    gov = self._gov()
    assert self._settle(gov, 50.0, 25.0, 27.0, 30.0, -0.5) == 30.0
    assert not gov.stopping_lead

  def test_a_braking_lead_arms_it_well_above_the_crawl_threshold(self):
    gov = self._gov()
    self._settle(gov, 45.0, 16.0, 20.0, 25.0, -2.5)
    assert gov.stopping_lead, "16 m/s is far above STOP_GOV_LEAD_V; only the braking arms it"
    assert gov.cap is not None

  def test_it_takes_several_frames_of_braking_to_commit(self):
    # A duration, not a magnitude: one hard aLeadK sample must not arm it.
    gov = self._gov()
    for _ in range(ls.STOP_GOV_DECEL_N - 1):
      gov.update(True, 45.0, 16.0, 20.0, 25.0, -3.0)
      assert not gov.stopping_lead
    gov.update(True, 45.0, 16.0, 20.0, 25.0, -3.0)
    assert gov.stopping_lead

  def test_one_clean_frame_disarms_the_claim(self):
    # The CLAIM is cheap to withdraw and expensive to make, which is the right
    # way round: withdrawing it only ever hands authority back to the MPC.
    gov = self._gov()
    self._settle(gov, 45.0, 16.0, 20.0, 25.0, -2.5)
    assert gov.stopping_lead
    gov.update(True, 45.0, 16.0, 20.0, 25.0, 0.0)
    assert not gov.stopping_lead

  def test_the_envelope_self_limits_on_a_highway_lead(self):
    # MEASURED, and this is the whole safety argument. A lead at 29 m/s braking
    # 0.8 m/s^2 fifty metres ahead projects its stop 576 m away; the cap that
    # comes back is 45 m/s, i.e. above anything this car will ever be doing.
    # ASSERTED AGAINST OUR OWN SPEED, which is the property that matters — a
    # cap only does something when it falls below what we are doing. A number
    # typed in here instead would pass or fail on where the constants happen to
    # sit rather than on whether the governor stays out of ordinary following.
    gov = self._gov()
    # 0.8 m/s^2 is under DECEL_MIN, so the projection is not used at all.
    assert gov.raw_cap(50.0, 29.0, 0.8) > 30.0
    # Above DECEL_MIN it IS used, and still comes back non-binding: the lead's
    # stop is 330 m away.
    assert gov.raw_cap(50.0, 29.0, 1.5) > 30.0
    # A lead braking gently from 20 m/s, 60 m ahead, while we do 20.
    assert gov.raw_cap(60.0, 20.0, 1.2) > 20.0

  def test_it_binds_where_the_geometry_says_it_must(self):
    # Lead braking 2.5 m/s^2 from 20 m/s, us at 20. The cap should cross under
    # our speed as the gap closes through the mid-40s, not at 6 m/s of lead
    # speed with the road already spent.
    gov = self._gov()
    assert gov.raw_cap(60.0, 20.0, 2.5) > 20.0
    assert gov.raw_cap(40.0, 20.0, 2.5) < 20.5

  def test_a_projected_stop_is_never_looser_than_the_old_envelope(self):
    # Believing a lead is stopping must only ever tighten the cap. If this
    # ever inverts, the feature has become a way to go FASTER at a braking car.
    gov = self._gov()
    for d in (20.0, 40.0, 60.0, 100.0):
      for vl in (5.0, 10.0, 20.0, 30.0):
        for dec in (1.2, 2.0, 3.0, 4.0):
          assert gov.raw_cap(d, vl, dec) <= gov.raw_cap(d, vl, 0.0) + 1e-9

  def test_zero_decel_is_bit_identical_to_the_old_envelope(self):
    gov = self._gov()
    for d in (15.0, 45.0, 120.0):
      for vl in (0.0, 4.0, 18.0):
        assert gov.raw_cap(d, vl, 0.0) == pytest.approx(
          math.sqrt(max(0.0, vl * vl + 2 * ls.STOP_GOV_A * max(0.0, d - ls.STOP_GOV_GAP_M))))

  def test_a_committed_stopper_is_not_faded_out(self):
    """The fade exists because a crawling lead may be about to accelerate away.
    That is not true of one under the brakes, and fading it would remove the
    authority exactly where this release adds it.

    DRIVEN AT THE TOP OF THE FADE BAND ON PURPOSE. At v_lead 5.9 the weight is
    (6.0 - 5.9) / 2.0 = 0.05, i.e. the cap is faded 95% of the way back to
    cruise and does essentially nothing. A first version of this test used 5.5,
    where the weight is 0.25 and the projected envelope is low enough to pass
    the assertion either way — vacuous against the mutation it exists to
    catch, which is the shape this repo has been bitten by repeatedly.
    """
    braking = self._gov()
    out = self._settle(braking, 40.0, 5.9, 20.0, 25.0, -2.5, n=40)
    assert braking.cap is not None
    # Unfaded, the envelope at 40 m behind a lead 5.9 m/s from a stop is well
    # under 20 m/s. Faded at w=0.05 it would sit just under cruise.
    assert out < 20.0, "the fade is swallowing a committed stopper's cap"

  def test_the_believed_lead_decel_is_clamped(self):
    """DRIVEN THROUGH `raw_cap` DIRECTLY, because the cap's own rate limiter
    hides it: FALL_RATE 3.0 m/s per second moves the published cap 0.15 m/s in
    a frame, so a single absurd `aLeadK` sample is invisible at the output
    while still being wrong at the envelope. Same lesson as v3.6.7's direction
    clamp, which survived two mutations for exactly this reason.

    `aLeadK` is an unclipped Kalman output on a radar track; a transient of
    -20 m/s^2 would otherwise project the lead's stop right on top of us.
    """
    gov = self._gov()
    at_cap = gov.raw_cap(40.0, 20.0, ls.STOP_GOV_DECEL_MAX)
    for absurd in (8.0, 20.0, 200.0):
      assert gov.raw_cap(40.0, 20.0, absurd) == pytest.approx(at_cap)
    # ...and the clamp is doing real work: an unclamped 20 would be far lower.
    unclamped = math.sqrt(2 * ls.STOP_GOV_A * (40.0 + 400.0 / 40.0 - ls.STOP_GOV_GAP_M))
    assert unclamped < at_cap - 1.0

  def test_it_still_only_acts_while_closing(self):
    # The v3.6.7 launch guard is untouched: a braking lead we are already
    # slower than is not our problem.
    gov = self._gov()
    assert self._settle(gov, 30.0, 16.0, 14.0, 25.0, -2.5) == 25.0
    assert gov.cap is None

  def test_it_can_only_ever_lower_the_cruise_target(self):
    gov = self._gov()
    for a in (0.0, -1.0, -2.5, -4.0, -9.0):
      out = self._settle(gov, 35.0, 12.0, 20.0, 25.0, a)
      assert out <= 25.0 + 1e-9

  def test_garbage_never_raises_and_never_arms(self):
    gov = self._gov()
    for a in (float('nan'), float('inf'), float('-inf')):
      gov.update(True, 40.0, 15.0, 20.0, 25.0, a)
      assert not gov.stopping_lead


class TestTheHiddenGovernorShavesOnlyTheSetSpeed:
  """FunnyPilot v3.7.1 — HIDDEN_CRUISE_OFFSET used to be applied AFTER the speed
  governors had taken their min(), so every SCC-V/SCC-M corner cap was also
  multiplied by 0.93: the dev panel said CAP 30 and the car held 27.9. Two
  speed reductions stacked on one corner. It now lands on the cruise candidate
  BEFORE `update_targets`, so a corner cap is honoured at the number the
  controller chose. Pinned on the AST: the planner imports acados."""

  def _update(self):
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / 'longitudinal_planner.py').read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'update')
    return fn

  def test_the_offset_is_applied_before_the_governors_and_never_after(self):
    import ast
    fn = self._update()
    body = ast.unparse(fn)
    shave = body.index("HIDDEN_CRUISE_OFFSET")
    governors = body.index("update_targets(")
    assert shave < governors, "the offset must shave the cruise CANDIDATE, before the governors"
    # ...and exactly once: nothing may re-apply it to the governed value
    assert body.count("HIDDEN_CRUISE_OFFSET") == 1, body.count("HIDDEN_CRUISE_OFFSET")
    # the shave is a multiplication of v_cruise, not something else wearing the name
    aug = [n for n in ast.walk(fn) if isinstance(n, ast.AugAssign)
           and "HIDDEN_CRUISE_OFFSET" in ast.unparse(n.value)]
    assert len(aug) == 1 and ast.unparse(aug[0].target) == "v_cruise"

  def test_the_governors_still_receive_a_cruise_value(self):
    """Anti-vacuous half: `update_targets` is still called with v_cruise."""
    import ast
    fn = self._update()
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and ast.unparse(n.func).endswith("update_targets"))
    assert "v_cruise" in [ast.unparse(a) for a in call.args]
