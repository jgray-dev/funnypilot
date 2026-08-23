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
  """v3.6.7 — VISION IS SUPREME WHERE VISION IS LOOKING.

  This class used to document the veto as DEAD, and it was: `MAP_SOLO_MAX_CUT *
  VISION_DISAGREE_TH` was under MAP_SOLO_MIN_CUT so the branch could not change
  any output, and `c` was floored by `learned` before the veto was evaluated so
  any recorded bend bypassed it outright. Both are reversed here, at the owner's
  request and for a concrete reason: where a one-lane way merges into two, the
  OSM geometry folds onto itself, every curvature estimator sees a corner, and
  the camera looking down an empty straight road is the only thing that knows
  better.
  """

  def test_the_veto_now_changes_outcomes_on_its_own(self):
    """The inverse of the old assertion, kept as an assertion so the reversal is
    explicit rather than implied by a deleted test. A cut that survives the
    veto's threshold must now be big enough to matter, or the veto is decorative
    again. MUTATION: put VISION_DISAGREE_TH back to 0.05."""
    assert MAP_SOLO_MAX_CUT * VISION_DISAGREE_TH > MAP_SOLO_MIN_CUT, (
      "the vision-disagreement veto is subsumed by MIN_CUT again and does nothing")

  def test_a_straight_road_the_map_calls_a_corner_gets_nothing(self):
    """THE REPORTED CASE. MUTATION: drop the veto, or restore the learned floor
    ahead of it."""
    for c in (0.0, 0.1, 0.25):
      v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=c,
                          learned_conf=0.0, dist_m=80.0, v_ego=25.0,
                          vision_available=True)
      assert v == CAP_INACTIVE, f"a corner the model denies got through at c={c}"

  def test_a_learned_record_no_longer_bypasses_it(self):
    """THE DELIBERATE REVERSAL, and the half that actually bit. The orphan
    learner files records at junctions the car once struggled at, and one
    completed pass is 0.45 — so before v3.6.7 any such record permanently
    outranked a camera looking straight down an empty road. A record says how
    FAST a bend is; it does not get to say one EXISTS where the model can see
    that it does not. MUTATION: evaluate the veto on `c` instead of
    `c_model`."""
    v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=0.0,
                        learned_conf=1.0, dist_m=80.0, v_ego=25.0,
                        vision_available=True)
    assert v == CAP_INACTIVE

  def test_the_veto_needs_scc_v_to_be_running(self):
    """LOAD-BEARING, NOT DEFENSIVE. `corroboration` is cleared to 0.0 in
    SCCVisionV2._reset(), so with the SCC-V toggle off or below its 5 m/s speed
    floor an ungated veto would silently disable SCC-M altogether — a far worse
    regression than the one it fixes. MUTATION: drop `vision_available`."""
    v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=0.0,
                        learned_conf=0.45, dist_m=80.0, v_ego=25.0,
                        vision_available=False)
    assert v < CAP_INACTIVE

  def test_a_real_corner_still_corroborates_far_above_the_threshold(self):
    """The threshold has to sit between straight-road noise and a real bend.
    `corroboration` is the lateral accel the model's path would pull at our
    speed over CORROB_FRAC * a_lat_target = 1.05 m/s^2, so 0.30 is 0.32 m/s^2 —
    a 2.3 km radius at 60 mph. Any bend worth slowing for saturates at 1.0."""
    assert 0.10 < VISION_DISAGREE_TH < 0.5
    v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=1.0,
                        learned_conf=0.0, dist_m=80.0, v_ego=25.0,
                        vision_available=True)
    assert v < CAP_INACTIVE

  def test_a_learned_record_still_grants_authority_where_vision_agrees(self):
    """The floor is only removed from the VETO, not from the authority sum. A
    bend the model can see and we have driven still gets full authority."""
    # a 3 m/s cut, under MAP_SOLO_MAX_CUT, so the ceiling does not bind and the
    # authority itself is what decides how much survives
    full = fuse_map_target(27.0, 30.0, vision_is_active=False, vision_corroboration=0.35,
                           learned_conf=1.0, dist_m=80.0, v_ego=25.0,
                           vision_available=True)
    part = fuse_map_target(27.0, 30.0, vision_is_active=False, vision_corroboration=0.35,
                           learned_conf=0.0, dist_m=80.0, v_ego=25.0,
                           vision_available=True)
    assert full == pytest.approx(27.0), "a driven corner the model sees passes whole"
    assert part > full, "...and without the record it is throttled by corroboration"

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
