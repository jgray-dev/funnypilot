"""FunnyPilot v3.6.2 — radius to speed, and the interval that gets learned.

The rules encoded here are the ones that would look identical in review if they
were inverted, so each of those has a test that names the mutation:

  * a clean pass may only ever RAISE the floor. Invert it and a single crawl
    behind a bus teaches the car that the bend is slow forever.
  * a stressed pass may only ever LOWER the ceiling. Invert it and a bend that
    just ran out of steering gets faster.
  * `min(a_lo, a_hi)`. Take the floor instead and a corner that has proved it
    cannot support 2.0 keeps being driven at the 2.4 it once managed.
  * the direction-dependent confidence weight. Make it symmetric and a corner
    known to be dangerous stays fast until its third visit.
  * seeding on the first visit. Drop it and one very bad pass moves the
    ceiling a third of the way, so the corner stays wrong for several more.

Import-light: stdlib only.
"""
import math

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_speed as CS


class TestTheEquation:
  def test_v_is_sqrt_a_r(self):
    assert CS.speed_for(100.0, 2.0) == pytest.approx(math.sqrt(200.0))
    assert CS.speed_for(400.0, 1.8) == pytest.approx(math.sqrt(720.0))

  def test_it_round_trips(self):
    for r in (30.0, 120.0, 500.0):
      for a in (1.0, 1.8, 3.0):
        assert CS.lat_accel_for(r, CS.speed_for(r, a)) == pytest.approx(a)

  def test_nonsense_is_zero_not_an_exception(self):
    for r, a in ((0.0, 2.0), (-5.0, 2.0), (100.0, 0.0), (float('nan'), 2.0),
                 (100.0, float('inf')), (None, 2.0), ("x", "y")):
      assert CS.speed_for(r, a) == 0.0

  def test_the_default_budget_leaves_room_to_learn_in_both_directions(self):
    """v3.6.5 — 1.8 -> 2.25, because a first visit was unbearably slow.

    WHAT THE DEFAULT HAS TO SATISFY IS A RELATIONSHIP, NOT A VALUE: it must sit
    strictly inside the learned bounds, so a corner can teach us it is slower
    AND a corner can earn its way faster. Pinning it at the ceiling would make
    the whole interval one-directional. MUTATION: set it to A_LAT_MAX."""
    assert CS.A_LAT_MIN < CS.A_LAT_DEFAULT < CS.A_LAT_MAX
    # ...and room above it worth having: a corner that proves itself must be
    # able to gain at least another 10% of speed over the default.
    assert CS.A_LAT_MAX / CS.A_LAT_DEFAULT >= 1.21

  def test_the_default_is_the_requested_step_over_v365(self):
    """Speed is sqrt(a*R), so the 10-15% FASTER that was asked for is 21-32% of
    BUDGET. Pinned in speed terms because that is the quantity that was
    specified and the one the driver feels."""
    faster = math.sqrt(CS.A_LAT_DEFAULT / 1.8)
    assert 1.10 <= faster <= 1.15


class TestTheIntervalCloses:
  def test_a_clean_fast_pass_raises_the_floor(self):
    lo, hi = CS.update_interval(1.8, 3.0, a_peak=2.4, severity=0.1)
    assert lo > 1.8
    assert hi == 3.0

  def test_a_clean_SLOW_pass_changes_nothing(self):
    """MUTATION: drop the `target > lo` guard. Following a bus round a bend at
    0.8 m/s^2 is not evidence that the bend is slow — it is evidence of a bus.
    THIS IS WHY SCC-M v2 NEEDS NO LEAD EXCLUSION: traffic cannot poison an
    interval that only moves on evidence it actually provides."""
    lo, hi = CS.update_interval(2.4, 3.0, a_peak=0.8, severity=0.0)
    assert lo == pytest.approx(2.4)
    assert hi == pytest.approx(3.0)

  def test_a_stressed_pass_lowers_the_ceiling(self):
    lo, hi = CS.update_interval(1.8, 3.0, a_peak=2.6, severity=1.4)
    assert hi < 3.0
    assert lo == pytest.approx(1.8)

  def test_a_stressed_pass_never_raises_the_ceiling(self):
    """MUTATION: drop the `target < hi` guard."""
    lo, hi = CS.update_interval(1.8, 1.5, a_peak=2.8, severity=1.1)
    assert hi <= 1.5 + 1e-9

  def test_the_middle_band_is_evidence_of_nothing(self):
    """A pass between CLEAN_TH and 1.0 was neither comfortable nor over the
    limit. Moving anything on it would be inventing information."""
    lo, hi = CS.update_interval(2.0, 2.8, a_peak=2.5, severity=0.75)
    assert (lo, hi) == pytest.approx((2.0, 2.8))

  def test_severity_divides_so_a_wild_pass_still_teaches(self):
    """REQUIREMENT 4, and the reason this is a division rather than a fixed
    margin. Drive a bend far too fast with nothing engaged: the reversal rate
    comes out at three times the threshold, so the pass says the corner
    supports about a third of what was just pulled. A fixed margin would say
    'a bit under 4.0' however badly it went, and the case where the car most
    needs to learn something is the one it would learn least from."""
    _lo, hi = CS.update_interval(1.8, 3.0, a_peak=4.0, severity=3.0, seed=True)
    assert hi == pytest.approx(4.0 / 3.0, abs=0.02)

  def test_bounds_are_respected_from_any_input(self):
    for peak in (0.0, 0.1, 5.0, 50.0, float('nan')):
      for sev in (0.0, 0.9, 1.0, 5.0, float('nan')):
        lo, hi = CS.update_interval(1.8, 3.0, peak, sev)
        assert CS.A_LAT_MIN <= lo <= CS.A_LAT_MAX
        assert CS.A_LAT_MIN <= hi <= CS.A_LAT_MAX


class TestSeedingTheFirstVisit:
  def test_a_first_pass_is_adopted_outright(self):
    """MUTATION: drop `seed`. A fresh record's bounds are not measurements —
    a_hi starts at A_LAT_MAX because nothing is known — so EMA-ing away from
    them treats ignorance as evidence."""
    seeded = CS.update_interval(1.8, 3.0, 3.6, 3.0, seed=True)[1]
    eased = CS.update_interval(1.8, 3.0, 3.6, 3.0, seed=False)[1]
    assert seeded == pytest.approx(1.2, abs=0.02)
    assert eased > seeded + 0.5, "the mutation must actually change something"

  def test_seeding_does_not_bypass_the_direction_guards(self):
    """Seeding changes how fast an admissible move is adopted, never which
    moves are admissible."""
    lo, _ = CS.update_interval(2.5, 3.0, a_peak=1.1, severity=0.0, seed=True)
    assert lo == pytest.approx(2.5), "a gentle pass must not lower the floor"
    _, hi = CS.update_interval(1.8, 1.4, a_peak=3.0, severity=2.0, seed=True)
    assert hi <= 1.4 + 1e-9, "a stressed pass must not raise the ceiling"


class TestBlendingWithTheDefault:
  def test_no_visits_is_exactly_the_default(self):
    assert CS.effective_a_lat(2.6, 3.0, 0) == pytest.approx(CS.A_LAT_DEFAULT)

  def test_a_ceiling_always_beats_a_floor(self):
    """MUTATION: use a_lo instead of min(a_lo, a_hi). A corner that once
    managed 2.4 and has since proved it cannot support 2.0 must be driven at
    2.0 — the newer, tighter bound wins and the floor is stale."""
    both = CS.effective_a_lat(2.4, 2.0, 5)
    assert both <= 2.0 + 1e-9

  def test_earning_speed_takes_visits(self):
    """A learned value ABOVE the default is a claim we may go faster than the
    geometry alone allows, so it is weighted by visit count."""
    one = CS.effective_a_lat(3.0, 3.0, 1)
    three = CS.effective_a_lat(3.0, 3.0, 3)
    assert CS.A_LAT_DEFAULT < one < three
    assert three == pytest.approx(3.0)

  def test_giving_speed_up_does_not(self):
    """MUTATION: make the weight symmetric. Withholding a slowdown until the
    third visit is not the conservative choice, and a corner known on its first
    pass to be dangerous must not stay fast for two more."""
    a = CS.effective_a_lat(1.2, 1.2, 1)
    assert a < CS.A_LAT_DEFAULT
    # at least CONF_LOWER_FLOOR of the way down on the very first visit
    assert a <= CS.A_LAT_DEFAULT - (CS.A_LAT_DEFAULT - 1.2) * CS.CONF_LOWER_FLOOR + 1e-9

  def test_the_result_is_always_in_bounds(self):
    for lo in (-5.0, 0.5, 1.8, 4.0, float('nan')):
      for hi in (-1.0, 1.0, 3.0, 9.0, float('nan')):
        for n in (0, 1, 3, 50):
          a = CS.effective_a_lat(lo, hi, n)
          assert CS.A_LAT_MIN <= a <= CS.A_LAT_MAX

  def test_confidence_is_monotone_and_saturates(self):
    prev = -1.0
    for n in range(8):
      c = CS.confidence_for(n)
      assert c >= prev
      prev = c
    assert CS.confidence_for(CS.CONF_FULL_VISITS) == pytest.approx(1.0)
    assert CS.confidence_for(100) == pytest.approx(1.0)


class TestTheApproachEnvelope:
  def test_at_the_corner_the_cap_is_the_corner_speed(self):
    assert CS.approach_cap(12.0, 0.0) == pytest.approx(12.0)

  def test_it_is_monotone_in_distance(self):
    """THE PROPERTY THE INTEGRAL EXISTS FOR (v3.5.6). Evaluate the budget at
    each point's own distance instead of integrating and the cap LOOSENS on
    some approaches as the corner closes — the car speeds back up mid-approach.
    MUTATION: replace the integral table with a flat budget times distance."""
    prev = 0.0
    d = 0.0
    while d <= 450.0:
      cap = CS.approach_cap(12.0, d)
      assert cap >= prev - 1e-9, f"cap fell going further out, at d={d}"
      prev = cap
      d += 2.0

  def test_the_corner_speed_is_reached_before_the_corner(self):
    """The arrival lead is in seconds of travel AT THE CORNER SPEED, so the cap
    reaches v_corner `v_corner * ARRIVAL_LEAD_T` metres early — corner ENTRY
    rather than apex, which is the v3.6.1 fix."""
    v = 11.0
    lead_m = v * CS.ARRIVAL_LEAD_T
    assert CS.approach_cap(v, lead_m) == pytest.approx(v)
    assert CS.approach_cap(v, lead_m + 40.0) > v

  def test_the_implied_deceleration_never_exceeds_what_the_mpc_can_do(self):
    """The envelope may not ask for more than the MPC can deliver. v3.6.5
    steepened the table to 1.35 after v3.6.5 raised CRUISE_MIN_ACCEL to -1.6;
    the RELATIONSHIP between the two is pinned by
    TestTheEnvelopeHasRoomToBeFollowed, which reads the constant out of
    long_mpc rather than restating it here."""
    v = 10.0
    d = 1.0
    while d < 400.0:
      a = (CS.approach_cap(v, d + 1.0) ** 2 - CS.approach_cap(v, d) ** 2) / 2.0
      assert a <= 1.601, f"implied decel {a:.3f} m/s^2 at d={d}"
      d += 1.0

  def test_v366_starts_later_than_v365_did(self):
    """The owner asked for "an extra 100 ft of cruise speed" instead of a long
    gas-gated coast. MEASURED on the reported case, 60 mph into a 29 mph bend:
    the cap first crosses 1 m/s under the set speed at ~327 m, where the v3.6.5
    table had it constraining from the moment the corner appeared at 400 m.
    MUTATION: restore _J_V to (0, 72, 144, 269)."""
    v_set, v_corner = 26.8, 13.0
    d = 400.0
    while d > 0.0 and CS.approach_cap(v_corner, d) > v_set - 1.0:
      d -= 1.0
    assert 280.0 <= d <= 360.0, f"first constrains at {d:.0f} m"

  def test_nonsense_never_constrains(self):
    for v, d in ((0.0, 100.0), (-3.0, 100.0), (float('nan'), 100.0),
                 (12.0, float('nan'))):
      assert CS.approach_cap(v, d) == float('inf') or CS.approach_cap(v, d) >= 12.0


class TestSettling:
  """FunnyPilot v3.6.2 — CONFIDENCE IS CONVERGENCE, NOT ATTENDANCE.

  `confidence_for(visits)` saturates at three visits, so a corner whose speed
  estimate was still moving 15% on its third pass counted as fully known. That
  is backwards: a pass that changes the answer is the strongest available
  evidence that the answer was not yet right. `settle_factor` measures whether
  successive passes agree, and `confidence_of` requires both.
  """

  def test_no_movement_is_fully_settled(self):
    assert CS.settle_factor(0.0) == pytest.approx(1.0)
    assert CS.settle_factor(CS.DRIFT_SETTLED) == pytest.approx(1.0)

  def test_large_movement_is_not_settled_at_all(self):
    assert CS.settle_factor(CS.DRIFT_LEARNING) == 0.0
    assert CS.settle_factor(CS.DRIFT_LEARNING * 10) == 0.0

  def test_it_is_monotone_between(self):
    xs = [i / 50.0 for i in range(int(CS.DRIFT_LEARNING * 50) + 5)]
    vals = [CS.settle_factor(x) for x in xs]
    assert vals == sorted(vals, reverse=True)

  def test_non_finite_drift_is_unsettled_not_settled(self):
    """MUTATION: return 1.0 for garbage. A corner whose convergence cannot be
    evaluated would then be handed full authority on the strength of a NaN --
    failing toward confident is the one direction that costs speed safety."""
    for bad in (float('nan'), float('inf'), None, "x"):
      assert CS.settle_factor(bad) == 0.0

  def test_the_users_case_a_third_pass_that_still_moves(self):
    """A third pass shifting the SPEED by 15% shifts `a` by 1.15^2 = 1.32x,
    i.e. ~0.6 m/s^2 at a typical budget. The old measure called that certain."""
    d = CS.DRIFT_UNKNOWN
    for moved in (0.6, 0.2, 0.58):
      d = CS.update_drift(d, moved)
    assert CS.confidence_for(3) == 1.0            # what it used to say
    assert CS.confidence_of(3, d) < 0.2           # what it says now

  def test_repeated_agreement_converges_to_certainty(self):
    """The other direction, or the measure would just be a way of never
    trusting anything. Ten identical passes must reach 1.0."""
    d = CS.DRIFT_UNKNOWN
    for _ in range(10):
      d = CS.update_drift(d, 0.0)
    assert CS.confidence_of(10, d) == pytest.approx(1.0)

  def test_drift_never_goes_negative(self):
    """It is an absolute movement; a negative one would invert settle_factor."""
    d = CS.DRIFT_UNKNOWN
    for moved in (-5.0, 0.0, -0.3):
      d = CS.update_drift(d, moved)
      assert d >= 0.0

  def test_movement_sign_does_not_matter(self):
    """Moving the answer down is exactly as much evidence of unsettledness as
    moving it up. MUTATION: drop the abs() and a corner that only ever slows
    would read as perfectly settled."""
    assert CS.update_drift(0.1, -0.5) == pytest.approx(CS.update_drift(0.1, 0.5))

  def test_visits_alone_are_not_enough(self):
    """The exact conflation this release exists to undo, stated directly."""
    assert CS.confidence_for(50) == 1.0
    assert CS.confidence_of(50, CS.DRIFT_LEARNING) == 0.0

  def test_settling_alone_is_not_enough_either(self):
    """A single pass has nothing to disagree with, so it trivially looks
    settled. Requiring both is what stops one lucky pass buying speed."""
    assert CS.settle_factor(0.0) == 1.0
    assert CS.confidence_of(1, 0.0) < 1.0

  def test_no_drift_argument_is_the_old_behaviour(self):
    """A caller with no drift history must get exactly the the first cut of v3.6.2 answer,
    which is what keeps this change confined to the callers that opted in."""
    for n in range(6):
      assert CS.confidence_of(n) == pytest.approx(CS.confidence_for(n))


class TestDriftCannotDelayASlowdown:
  """THE SAFETY PROPERTY OF THE WHOLE RELEASE, and it is structural rather
  than numerical: `drift` enters `effective_a_lat` only through `c`, and the
  lowering branch floors `c` at CONF_LOWER_FLOOR. So convergence can make the
  car earn speed more slowly and can do nothing else.
  """

  def test_a_slowdown_is_identical_however_unsettled(self):
    """MUTATION: apply the confidence weight symmetrically (drop the
    `max(c, CONF_LOWER_FLOOR)` branch) and an unsettled corner would stop
    being allowed to tell us it is slow."""
    settled = CS.effective_a_lat(1.2, 1.2, visits=1, drift=0.0)
    unsettled = CS.effective_a_lat(1.2, 1.2, visits=1, drift=CS.DRIFT_LEARNING)
    assert settled == pytest.approx(unsettled)
    assert unsettled < CS.A_LAT_DEFAULT

  def test_garbage_drift_still_cannot_delay_a_slowdown(self):
    for bad in (float('nan'), float('inf')):
      assert CS.effective_a_lat(1.2, 1.2, visits=1, drift=bad) < CS.A_LAT_DEFAULT

  def test_an_unsettled_corner_cannot_buy_speed(self):
    """The other half. MUTATION: pass `visits` where `drift` belongs, or drop
    the drift argument at the call site in scc_map_v2._lookup."""
    fast = CS.effective_a_lat(2.6, 3.0, visits=3, drift=CS.DRIFT_LEARNING)
    assert fast == pytest.approx(CS.A_LAT_DEFAULT)

  def test_a_settled_corner_does_buy_speed(self):
    fast = CS.effective_a_lat(2.6, 3.0, visits=3, drift=0.0)
    assert fast > CS.A_LAT_DEFAULT


class TestTheRunOut:
  """FunnyPilot v3.6.5 — a corner has an EXIT, and the cap has to know it.

  Before this, the cap for a corner already behind us was
  `approach_cap(v, max(distance, 0))` — a FLAT `v_corner` right up until
  `_refresh_corners` dropped the corner at BEHIND_KEEP_M past its exit, at
  which point the constraint vanished. So the car sat at corner speed on
  straightening road and then got its throttle back all at once, which is the
  reported "noticeable delay in reapplying throttle to accelerate out".
  """
  V, HALF = 13.0, 20.0        # a 29 mph bend, 40 m long

  def test_the_apex_is_the_corner_speed(self):
    assert CS.corner_cap(self.V, 0.0, self.HALF) == pytest.approx(self.V)

  def test_the_whole_arc_holds_the_corner_speed(self):
    """The measured radius applies across the detected run, so the speed limit
    does too. MUTATION: release from the apex (ignore half_len) and the car is
    asked to accelerate while it is still turning."""
    for d in (self.HALF, 5.0, 0.0, -5.0, -self.HALF):
      assert CS.corner_cap(self.V, d, self.HALF) == pytest.approx(self.V), d

  def test_authority_returns_progressively_past_the_exit(self):
    """THE FIX. MUTATION: clamp the distance at zero again, i.e.
    `approach_cap(v, max(d, 0))`. Every value below collapses to V and the
    assertion that they are strictly increasing fails."""
    caps = [CS.corner_cap(self.V, -(self.HALF + s), self.HALF)
            for s in (0.0, 10.0, 20.0, 40.0, 80.0)]
    assert caps[0] == pytest.approx(self.V)
    assert caps == sorted(caps)
    assert all(b > a for a, b in zip(caps, caps[1:], strict=False))
    # and it is the release ramp, not something invented
    assert caps[2] == pytest.approx(math.sqrt(self.V ** 2 + 2 * CS.RELEASE_RATE * 20.0))

  def test_the_flat_hold_that_was_measured_is_gone(self):
    """The specific number from the report: a 40 m bend used to hold corner
    speed for BEHIND_KEEP_M = 25 m past its exit. It now hands back 2.2 m/s
    (5 mph) over that same stretch."""
    at_exit = CS.corner_cap(self.V, -self.HALF, self.HALF)
    at_keep = CS.corner_cap(self.V, -(self.HALF + 25.0), self.HALF)
    assert at_keep - at_exit > 2.0

  def test_the_run_out_is_briskerthan_the_run_in(self):
    """2.5 m/s^2 out against a budget capped at 1.20 in: 'brake early,
    accelerate out'. MUTATION: use the approach budget for the exit."""
    out = CS.corner_cap(self.V, -(self.HALF + 60.0), self.HALF)
    in_ = CS.corner_cap(self.V, self.HALF + 60.0, self.HALF)
    assert out > in_

  def test_it_matches_approach_cap_ahead_of_the_bend(self):
    """Nothing about the approach changed; only the far side did."""
    for d in (50.0, 150.0, 400.0):
      assert CS.corner_cap(self.V, d + self.HALF, self.HALF) == \
             pytest.approx(CS.approach_cap(self.V, d + self.HALF))

  def test_garbage_never_constrains(self):
    for v, d, h in ((0.0, 10.0, 5.0), (float('nan'), 10.0, 5.0),
                    (12.0, float('inf'), 5.0)):
      assert CS.corner_cap(v, d, h) == float('inf')

  def test_a_missing_half_length_degrades_to_the_apex(self):
    """An unknown extent must not invent one. Releasing from the apex is the
    conservative reading of 'we do not know how long this bend is'... in the
    sense that it matches the pre-v3.6.5 geometry rather than extending the
    hold on a number we do not have."""
    assert CS.corner_cap(self.V, -10.0, 0.0) == \
           pytest.approx(math.sqrt(self.V ** 2 + 2 * CS.RELEASE_RATE * 10.0))

  def test_the_release_rate_is_the_smoothers_own(self):
    """Imported, not mirrored: CurveSpeedCap will throttle the cap to this rate
    anyway, so a raw envelope climbing faster would be silently clipped and one
    climbing slower would be the binding constraint without saying so."""
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import curve_cap
    assert CS.RELEASE_RATE is curve_cap.RELEASE_RATE


class TestTheEnvelopeHasRoomToBeFollowed:
  """FunnyPilot v3.6.5 — THE INVARIANT THAT WAS BROKEN, pinned on the source.

  `long_mpc` clips the cruise target to `v_ego + T_IDXS * CRUISE_MIN_ACCEL *
  1.05` before it ever solves, so that product is a hard ceiling on how fast
  ANY falling cruise target — including SCC-M v2's cap — can slow the car.

  v3.6.4 set the envelope's steepest segment to 1.20 and CRUISE_MIN_ACCEL to
  -1.2, i.e. the envelope asked for 95% of what the MPC was allowed to deliver.
  With that little margin the car could not catch up once it fell behind for
  any reason, so it never got back on schedule and arrived at the bend above
  the cap the envelope had been asking for since 400 m out. Two constants that
  each looked right and were wrong TOGETHER, in different files.

  AST, NOT A RUNTIME CHECK: long_mpc imports acados and cannot be constructed
  off-device, which is the same reason the gas-gate wiring is pinned this way.
  """
  MPC_FACTOR = 1.05      # long_mpc's own multiplier on the clip
  # The envelope may ask for at most this share of what the MPC can deliver.
  # 1.20 / (1.6 * 1.05) = 0.71 today; the defect was 1.20 / (1.2 * 1.05) = 0.95.
  MAX_SHARE = 0.85

  def _cruise_min_accel(self):
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[6] / \
        "selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
      if isinstance(node, ast.Assign) and any(
          isinstance(t, ast.Name) and t.id == "CRUISE_MIN_ACCEL" for t in node.targets):
        return float(ast.literal_eval(node.value))
    raise AssertionError("CRUISE_MIN_ACCEL not found — this scan is vacuous")

  def _steepest_budget(self):
    return max((CS._J_V[i] - CS._J_V[i - 1]) / (CS._J_BP[i] - CS._J_BP[i - 1])
               for i in range(1, len(CS._J_BP)))

  def test_the_scan_actually_found_the_constant(self):
    """Anti-vacuous: a scan that silently finds nothing passes everything."""
    a = self._cruise_min_accel()
    assert a < 0.0 and math.isfinite(a)

  def test_the_envelope_is_steepest_nearest_the_corner(self):
    """Brake hardest last. A non-monotone budget would make the cap loosen as
    the bend closes, which is the v3.5.6 defect this table exists to avoid."""
    slopes = [(CS._J_V[i] - CS._J_V[i - 1]) / (CS._J_BP[i] - CS._J_BP[i - 1])
              for i in range(1, len(CS._J_BP))]
    assert slopes == sorted(slopes, reverse=True)

  def test_the_mpc_can_actually_follow_the_envelope(self):
    """MUTATION: put CRUISE_MIN_ACCEL back to -1.2, or steepen _J_V's first
    segment past ~1.35. Either alone reproduces the defect."""
    available = abs(self._cruise_min_accel()) * self.MPC_FACTOR
    asked = self._steepest_budget()
    assert asked < available, "the envelope asks for more than the MPC may give"
    assert asked / available <= self.MAX_SHARE, \
        f"only {100 * (1 - asked / available):.0f}% headroom; the car cannot catch up"
