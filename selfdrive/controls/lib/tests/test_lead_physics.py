"""FunnyPilot v3.5.5 — lead stopping-distance physics.

The load-bearing test in here is `TestOrdinaryFollowingIsUntouched`: the whole
safety argument for shipping this without a drive is that a lead which is not
braking harder than COMFORT_BRAKE gets a BIT-IDENTICAL answer, so the reported
"constantly braking when following a lead that is simply slowing down" cannot
be made worse by construction.
"""
import math

import pytest

from openpilot.selfdrive.controls.lib.lead_physics import (LEAD_DECEL_MAX, LeadDecelBelief,
                                                           believed_lead_decel,
                                                           stopped_equivalence_distance)

COMFORT = 2.2


class TestOrdinaryFollowingIsUntouched:
  """A lead that is coasting, holding speed, accelerating, or easing off more
  gently than we would must return EXACTLY the pre-v3.5.5 number."""

  @pytest.mark.parametrize("a_lead", [3.0, 1.0, 0.0, -0.5, -1.5, -2.19, -2.2])
  def test_gentle_or_no_braking_is_a_no_op(self, a_lead):
    assert believed_lead_decel(a_lead, COMFORT) == COMFORT

  def test_the_distance_matches_the_old_formula_there(self):
    for v in (0.0, 5.0, 13.4, 20.0, 31.3):
      assert stopped_equivalence_distance(v, believed_lead_decel(-1.0, COMFORT)) == \
             pytest.approx(v ** 2 / (2 * COMFORT))

  def test_a_filter_that_never_sees_hard_braking_never_moves(self):
    b = LeadDecelBelief(COMFORT)
    for _ in range(200):
      b.update(-1.0, True)
    assert b.x == COMFORT


class TestABrakingLeadIsBelieved:

  def test_harder_braking_shortens_the_equivalence_distance(self):
    """MUTATION: min() instead of max() in the floor, or dividing instead of
    multiplying. Both invert the direction and read as plausible edits."""
    gentle = stopped_equivalence_distance(20.0, believed_lead_decel(-1.0, COMFORT))
    hard = stopped_equivalence_distance(20.0, believed_lead_decel(-4.0, COMFORT))
    assert hard < gentle
    # ABSOLUTE, not relative: a mutation that scales both sides together would
    # sail through a comparison of two calls (the v3.5.4 process lesson).
    assert gentle == pytest.approx(400 / (2 * 2.2), abs=0.01)
    assert hard == pytest.approx(400 / (2 * 4.0), abs=0.01)

  def test_the_worked_example_from_the_docstring(self):
    """20 m/s, lead braking at 4 m/s^2. The old assumption placed the obstacle
    90.9 m past the lead; the truth is 50 m. That ~41 m is the entire reason
    the stop had to be completed harder than COMFORT_BRAKE."""
    old = stopped_equivalence_distance(20.0, COMFORT)
    new = stopped_equivalence_distance(20.0, believed_lead_decel(-4.0, COMFORT))
    assert old - new == pytest.approx(40.9, abs=0.2)

  def test_it_can_never_place_the_obstacle_further_away(self):
    """SAFETY INVARIANT: no input may make us brake LATER than v3.5.4 did."""
    for a in (-12.0, -6.0, -4.0, -2.2, -1.0, 0.0, 2.0, 9.0):
      assert believed_lead_decel(a, COMFORT) >= COMFORT

  def test_belief_is_capped(self):
    """Not a safety bound — a NOISE bound. `aLeadK` is a Kalman output on a
    radar track, and an uncapped spike would yank the obstacle tens of metres
    closer for one frame, i.e. the brake jab this change exists to remove."""
    assert believed_lead_decel(-40.0, COMFORT) == LEAD_DECEL_MAX
    assert believed_lead_decel(-1e9, COMFORT) == LEAD_DECEL_MAX


class TestGarbageDegradesToTheOldNumber:

  @pytest.mark.parametrize("junk", [None, "x", float('nan'), float('inf'), float('-inf')])
  def test_bad_accel_reads_as_no_extra_belief(self, junk):
    assert believed_lead_decel(junk, COMFORT) == COMFORT

  def test_a_bad_comfort_brake_never_divides_by_zero(self):
    for bad in (0.0, -1.0, float('nan'), None):
      d = believed_lead_decel(-4.0, bad)
      assert d > 0.0 and math.isfinite(d)


class TestTheFilter:

  def test_it_is_seeded_at_the_old_assumption(self):
    """A fresh track's first aLeadK samples are its least reliable, so a lead
    that has just appeared is credited with nothing extra until it has been
    observed braking for a moment."""
    b = LeadDecelBelief(COMFORT)
    assert b.x == COMFORT
    assert b.update(-4.0, True) < LEAD_DECEL_MAX

  def test_it_converges_on_a_sustained_demand(self):
    b = LeadDecelBelief(COMFORT)
    for _ in range(200):
      b.update(-4.0, True)
    assert b.x == pytest.approx(LEAD_DECEL_MAX, abs=1e-3)

  def test_a_single_noisy_frame_barely_moves_it(self):
    """The jab guard. One spurious -10 must not be worth more than a few
    percent of the obstacle position."""
    b = LeadDecelBelief(COMFORT)
    for _ in range(100):
      b.update(-1.0, True)
    before = b.x
    b.update(-10.0, True)
    assert (b.x - before) / (LEAD_DECEL_MAX - COMFORT) < 0.25

  def test_losing_the_lead_resets_the_belief(self):
    b = LeadDecelBelief(COMFORT)
    for _ in range(200):
      b.update(-4.0, True)
    b.update(-4.0, False)
    assert b.x == COMFORT

  def test_it_stays_within_bounds_under_adversarial_input(self):
    b = LeadDecelBelief(COMFORT)
    for a in (-9.0, 4.0, float('nan'), -3.0, None, 0.0, -1e6, 2.0):
      b.update(a, True)
      assert COMFORT <= b.x <= LEAD_DECEL_MAX
