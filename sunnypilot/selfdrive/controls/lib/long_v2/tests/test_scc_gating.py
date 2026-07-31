"""FunnyPilot v3.4.9 — merged SCC arbitration.

v3.3.8 made SCC-M's cap conditional on SCC-V being independently ACTIVE. The
protection that bought (a bad map point on a straight road cannot brake the car)
is preserved EXACTLY; what changes is that corroboration is now continuous, so a
real corner the model can plainly see no longer has to clear vision's own
comfort threshold before the map may act on it.

Each test names the mutation it guards.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.speed_governor import gate_map_target
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import (
  fuse_map_target, MAP_SOLO_MAX_CUT, MAP_SOLO_MIN_CUT,
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

  def test_partial_corroboration_takes_a_proportional_cut(self):
    v = fuse_map_target(20.0, 30.0, vision_is_active=False, vision_corroboration=0.5)
    assert abs(v - 25.0) < 1e-9  # half of the 10 m/s the map asked for

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
