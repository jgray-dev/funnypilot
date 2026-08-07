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

  def test_the_default_budget_is_below_sccv(self):
    """1.8 is deliberately under SCC-V's own 2.1 comfort target: an unvisited
    corner's radius comes from OSM node geometry, and the cost of that being
    wrong should be paid in a slow corner, not a fast one. Learning is what
    earns it back."""
    assert CS.A_LAT_DEFAULT < 2.1
    assert CS.A_LAT_MIN < CS.A_LAT_DEFAULT < CS.A_LAT_MAX


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
    """1.20 m/s^2 is a structural ceiling: long_mpc.CRUISE_MIN_ACCEL is -1.2,
    so a speed cap falling faster than that is theatre the MPC cannot follow.
    MUTATION: raise the last entry of _J_V without raising CRUISE_MIN_ACCEL."""
    v = 10.0
    d = 1.0
    while d < 400.0:
      a = (CS.approach_cap(v, d + 1.0) ** 2 - CS.approach_cap(v, d) ** 2) / 2.0
      assert a <= 1.201, f"implied decel {a:.3f} m/s^2 at d={d}"
      d += 1.0

  def test_nonsense_never_constrains(self):
    for v, d in ((0.0, 100.0), (-3.0, 100.0), (float('nan'), 100.0),
                 (12.0, float('nan'))):
      assert CS.approach_cap(v, d) == float('inf') or CS.approach_cap(v, d) >= 12.0
