"""FunnyPilot v3.4.9 — merged SCC arbitration.

v3.3.8 made SCC-M's cap conditional on SCC-V being independently ACTIVE. The
protection that bought (a bad map point on a straight road cannot brake the car)
is preserved EXACTLY; what changes is that corroboration is now continuous, so a
real corner the model can plainly see no longer has to clear vision's own
comfort threshold before the map may act on it.

Each test names the mutation it guards.
"""
import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.speed_governor import gate_map_target
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import (
  fuse_map_target, MAP_SOLO_MAX_CUT, MAP_SOLO_MIN_CUT, VISION_DISAGREE_TH,
)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.corner_speed import confidence_for
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE


class TestStraightRoadVetoIntact:
  """The v3.3.8 protection. MUTATION: let the map through with no corroboration."""

  def test_map_alone_on_a_straight_road_is_ignored(self):
    assert fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=0.0) == CAP_INACTIVE

  def test_legacy_two_argument_call_still_vetoes(self):
    # the old call shape defaults to zero corroboration, i.e. the old behaviour
    assert gate_map_target(20.0, vision_is_active=False) == CAP_INACTIVE

  def test_map_inactive_stays_inactive_either_way(self):
    assert fuse_map_target(CAP_INACTIVE, 30.0, True, 1.0) == CAP_INACTIVE
    assert fuse_map_target(CAP_INACTIVE, 30.0, False, 1.0) == CAP_INACTIVE
    assert gate_map_target(CAP_INACTIVE, vision_is_active=True) == CAP_INACTIVE


class TestFullVisionActivationUnchanged:
  def test_map_confirmed_by_vision_passes_through(self):
    assert fuse_map_target(20.0, 30.0, vision_is_active=True, vision_corroboration=0.0) == 20.0
    assert gate_map_target(20.0, vision_is_active=True) == 20.0

  def test_active_vision_ignores_the_solo_ceiling(self):
    # a confirmed corner may take as much as the map asks for
    assert fuse_map_target(5.0, 30.0, vision_is_active=True, vision_corroboration=1.0) == 5.0


class TestGradedAuthority:
  """MUTATION: make corroboration binary again (any c > 0 -> full cut)."""

  def test_partial_corroboration_bounds_the_cut(self):
    """v3.5.6: authority is a CEILING, not a scale factor. A big ask at half
    corroboration is clipped to half of MAP_SOLO_MAX_CUT; a SMALL ask under
    that ceiling now passes through whole, which is what lets an early,
    gentle approach happen at all (see scc_fusion.py)."""
    v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=0.5)
    assert abs(v - (30.0 - MAP_SOLO_MAX_CUT * 0.5)) < 1e-9
    # the small-ask case, which the old `cut * c` multiplied away to nothing
    small = fuse_map_target(28.8, 30.0, vision_is_active=False, vision_corroboration=0.5)
    assert abs(small - 28.8) < 1e-9

  def test_full_corroboration_takes_the_whole_cut(self):
    v = fuse_map_target(25.0, 30.0, vision_is_active=False, vision_corroboration=1.0)
    assert abs(v - 25.0) < 1e-9

  def test_authority_is_monotone_in_corroboration(self):
    prev = 1e9
    for c in (0.2, 0.4, 0.6, 0.8, 1.0):
      v = fuse_map_target(15.0, 30.0, False, c)
      assert v <= prev + 1e-9
      prev = v

  def test_tiny_cuts_are_dropped_entirely(self):
    """A cut smaller than MIN_CUT is noise; reporting it would also make the
    planner attribute the plan source to the map for no reason."""
    assert fuse_map_target(30.0 - MAP_SOLO_MIN_CUT / 2, 30.0, False, 1.0) == CAP_INACTIVE

  def test_uncorroborated_cut_is_bounded(self):
    """MUTATION: drop MAP_SOLO_MAX_CUT. This is the cost ceiling on a map error
    that happens to coincide with any curvature at all."""
    v = fuse_map_target(5.0, 30.0, vision_is_active=False, vision_corroboration=1.0)
    assert abs(v - (30.0 - MAP_SOLO_MAX_CUT)) < 1e-9

  def test_corroboration_is_clamped(self):
    assert fuse_map_target(20.0, 30.0, False, 5.0) == fuse_map_target(20.0, 30.0, False, 1.0)
    assert fuse_map_target(20.0, 30.0, False, -1.0) == CAP_INACTIVE

  def test_map_never_raises_the_cruise_speed(self):
    """MUTATION: forget the max(0, ...) on the cut. A map point ABOVE cruise
    must not come back as a target above cruise."""
    assert fuse_map_target(40.0, 30.0, False, 1.0) == CAP_INACTIVE


class TestTheVisionDisagreementVeto:
  """v3.5.9's junction-jog protection, and what mutation testing said about it.

  THE BRANCH CANNOT CHANGE ANY OUTPUT AT THE CURRENT CONSTANTS. Replacing the
  whole veto condition with `if False:` leaves the suite green and leaves the
  car's behaviour unchanged, because a cut that only just survives the veto's
  own threshold is already under MAP_SOLO_MIN_CUT and dropped a few lines
  later. That is not a reason to write a contrived test that pretends
  otherwise — it is a reason to pin the arithmetic, so that changing either
  constant makes the veto start doing real work instead of quietly removing a
  protection nobody realised was already absent.
  """

  def test_the_veto_is_currently_subsumed_by_min_cut(self):
    """The relationship, not the values. If someone lowers MAP_SOLO_MIN_CUT or
    raises VISION_DISAGREE_TH, this fails and the veto becomes load-bearing —
    at which point it needs behavioural tests, which is exactly what the
    failure message should prompt."""
    assert MAP_SOLO_MAX_CUT * VISION_DISAGREE_TH < MAP_SOLO_MIN_CUT, (
      "the vision-disagreement veto now changes outcomes on its own"
      + " - give it behavioural tests")

  def test_an_unvisited_corner_the_model_says_is_straight_gets_nothing(self):
    """The behaviour the veto expresses, whichever check actually delivers it:
    where two lanes merge the OSM way jogs sideways, any curvature estimator
    sees a corner, and the model looking straight at it is the only thing that
    says otherwise."""
    for c in (0.0, 0.01, 0.04):
      v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=c,
                          learned_conf=0.0, dist_m=80.0, v_ego=25.0)
      assert v == CAP_INACTIVE, f"a corner the model denies got through at c={c}"

  def test_a_corner_we_have_driven_is_not_vetoed(self):
    """THE BYPASS, and it is `max(c, learned)` — nothing else. A bend does not
    stop existing because the model has not seen it over a crest. MUTATION:
    assign learned_conf instead of max()-ing it, or drop the floor entirely."""
    v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=0.0,
                        learned_conf=0.45, dist_m=80.0, v_ego=25.0)
    assert v < CAP_INACTIVE
    assert v < 30.0

  def test_one_visit_clears_the_veto_threshold_by_construction(self):
    """Why the bypass needs no clause of its own: confidence_for(1) is 0.45
    against a 0.05 threshold. An earlier draft added `and learned <
    LEARNED_TRUST_TH` to the veto, which reads like the bypass and could never
    fire — dead code that looks like a live knob."""
    assert confidence_for(1) > VISION_DISAGREE_TH * 5

  def test_beyond_the_model_horizon_proximity_still_stands_in(self):
    """The model's plan reaches ~v_ego * MODEL_HORIZON_T. Past that its silence
    is not evidence, and SCC-M v2 is the only thing that knows."""
    v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=0.0,
                        learned_conf=0.0, dist_m=250.0, v_ego=25.0)
    assert v < CAP_INACTIVE

  def test_a_learned_record_only_floors_and_never_lowers_authority(self):
    """MUTATION: assign learned_conf instead of max()-ing it. A corner the
    model can plainly see would be DOWNGRADED to its visit count."""
    seen = fuse_map_target(20.0, 30.0, False, 1.0, learned_conf=0.45,
                           dist_m=80.0, v_ego=25.0)
    unseen = fuse_map_target(20.0, 30.0, False, 1.0, learned_conf=0.0,
                             dist_m=80.0, v_ego=25.0)
    assert seen == pytest.approx(unseen)
