"""FunnyPilot v3.5.0 — advisory limits as evidence, and the SCC debug channel.

The rule these tests pin, in one line: a posted advisory speed may LOWER the
cap and may RAISE the map's authority, and may never do the opposite of either.

`MapAdvisoryLimit` is OSM `maxspeed:advisory` — a speed a highway engineer
surveyed and signed, as opposed to the geometry mapd computes. That makes it
the best available answer to "is this corner real", which is the exact question
the vision veto exists to ask. It is deliberately NOT a target: advisory tags
are per-WAY, so obeying one literally would hold the car down through the
straights between a curvy road's bends.
"""
import time

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_map_v2 import (
  SCCMapV2, ADVISORY_MARGIN, ADVISORY_MAX_CUT, _ADVISORY_MIN,
)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import (
  fuse_map_target, ADVISORY_CORROB_FLOOR, MAP_SOLO_MAX_CUT,
)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm


class NoParams:
  def get_bool(self, key):
    return True


def route_north(v_by_index, spacing_deg=0.0005):
  return [{"latitude": i * spacing_deg, "longitude": 0.0, "velocity": v}
          for i, v in enumerate(v_by_index)]


def make(points, advisory=(0.0, 0.0, 0.0), pos=(0.0, 0.0)):
  return SCCMapV2(params=NoParams(),
                  position_reader=lambda: pos,
                  velocities_reader=lambda: points,
                  advisory_reader=lambda: advisory)


def run(scc, v_ego=30., v_cruise=30., fric=0.8, n=1):
  for _ in range(n):
    scc.update({}, True, False, v_ego, 0.0, v_cruise, fric)


class TestAdvisoryOnlyEverLowers:
  def test_no_advisory_is_an_exact_no_op(self):
    """MUTATION: return something other than CAP_INACTIVE with no advisory."""
    straight = route_north([30] * 8)
    a = make(straight, advisory=(0.0, 0.0, 0.0))
    b = make(straight, advisory=(0.0, 0.0, 0.0))
    run(a, n=20)
    run(b, n=20)
    assert a.raw_v_target == b.raw_v_target
    assert not a.advisory_active

  def test_an_advisory_cannot_raise_a_geometry_cap(self):
    """MUTATION: use max() instead of min() when folding the advisory in.

    The route has a genuinely slow curve; a generous advisory must not be
    allowed to talk the cap back up.
    """
    points = route_north([30, 30, 30, 10, 30, 30])
    tight = make(points, advisory=(0.0, 0.0, 0.0))
    loose = make(points, advisory=(28.0, 0.0, 0.0))   # ~63 mph advisory
    run(tight, n=200)
    run(loose, n=200)
    assert loose.raw_v_target <= tight.raw_v_target + 1e-6

  def test_an_advisory_lowers_a_cap_geometry_missed(self):
    """The whole point: mapd's curve math sees nothing, the sign does."""
    straight = route_north([30] * 8)
    blind = make(straight, advisory=(0.0, 0.0, 0.0))
    seeing = make(straight, advisory=(13.4, 0.0, 0.0))  # 30 mph advisory
    run(blind, n=5)
    run(seeing, n=5)
    assert blind.raw_v_target == CAP_INACTIVE
    assert seeing.raw_v_target < 30.0
    assert seeing.advisory_active

  def test_margin_is_applied_so_we_do_not_crawl(self):
    """Advisory speeds are signed conservatively, so the cap sits ADVISORY_MARGIN
    above the number on the sign rather than on it. Cruise is low here so the
    MAX_CUT bound is slack and the margin is what decides the answer."""
    scc = make(route_north([16] * 8), advisory=(13.4, 0.0, 0.0))
    run(scc, v_ego=16., v_cruise=16., n=5)
    assert abs(scc.raw_v_target - 13.4 * ADVISORY_MARGIN) < 0.5

  def test_the_two_bounds_compose_and_the_tighter_one_wins(self):
    """At highway cruise the MAX_CUT bound is the binding one, not the margin —
    an advisory alone cannot take more than ADVISORY_MAX_CUT however low the
    sign is. Pinned because the two bounds are easy to reason about separately
    and easy to get wrong together."""
    scc = make(route_north([30] * 8), advisory=(13.4, 0.0, 0.0))
    run(scc, v_cruise=30., n=5)
    assert abs(scc.raw_v_target - (30.0 - ADVISORY_MAX_CUT)) < 1e-6

  def test_a_nonsense_low_advisory_is_ignored(self):
    """MUTATION: drop _ADVISORY_MIN. A mistagged 5 km/h advisory on a highway
    would otherwise ask for a stop."""
    scc = make(route_north([30] * 8), advisory=(_ADVISORY_MIN - 0.5, 0.0, 0.0))
    run(scc, n=5)
    assert scc.raw_v_target == CAP_INACTIVE

  def test_the_cut_is_bounded(self):
    """MUTATION: drop ADVISORY_MAX_CUT. This is the ceiling on what a single
    mistagged advisory can cost, and it is the reason this signal is allowed
    anywhere near the controller."""
    scc = make(route_north([30] * 8), advisory=(5.0, 0.0, 0.0))
    run(scc, v_cruise=31.0, n=5)
    assert scc.raw_v_target >= 31.0 - ADVISORY_MAX_CUT - 1e-6

  def test_an_advisory_ahead_tightens_with_distance(self):
    """MUTATION: apply the ahead-advisory as a step instead of through the
    approach envelope. The car would brake at the sign instead of before it."""
    far = make(route_north([30] * 8), advisory=(0.0, 13.4, 400.0))
    near = make(route_north([30] * 8), advisory=(0.0, 13.4, 60.0))
    run(far, n=5)
    run(near, n=5)
    assert near.raw_v_target < far.raw_v_target

  def test_a_broken_reader_is_survivable(self):
    """The reader touches /dev/shm on a moving car; it must never raise into
    the planner."""
    def boom():
      raise RuntimeError("mem params gone")
    scc = SCCMapV2(params=NoParams(), position_reader=lambda: (0.0, 0.0),
                   velocities_reader=lambda: route_north([30] * 8),
                   advisory_reader=boom)
    run(scc, n=5)
    assert scc.raw_v_target == CAP_INACTIVE
    assert not scc.advisory_active


class TestAdvisoryAsCorroboration:
  def test_advisory_floors_the_maps_authority(self):
    """MUTATION: ignore advisory_active in fuse_map_target.

    Vision has not seen the bend, so plain corroboration is near zero and the
    map would be vetoed. A surveyed advisory does not stop being evidence
    because the model is not looking yet.
    """
    without = fuse_map_target(20.0, 30.0, False, 0.0, advisory_active=False)
    with_adv = fuse_map_target(20.0, 30.0, False, 0.0, advisory_active=True)
    assert without == CAP_INACTIVE, "no corroboration at all must still veto"
    # the cut it asked for is 10 m/s; the floor lets ADVISORY_CORROB_FLOOR of
    # that through, still under the MAP_SOLO_MAX_CUT ceiling
    expected = 30.0 - min(10.0 * ADVISORY_CORROB_FLOOR, MAP_SOLO_MAX_CUT)
    assert abs(with_adv - expected) < 1e-6

  def test_it_is_a_floor_not_a_ceiling(self):
    """MUTATION: assign corroboration from the advisory instead of max()-ing it.

    Where vision already corroborates fully, the advisory must change nothing —
    it can only ever raise a low corroboration, never pull a high one down to
    ADVISORY_CORROB_FLOOR.
    """
    for c in (0.7, 0.9, 1.0):
      assert (fuse_map_target(20.0, 30.0, False, c, advisory_active=True) ==
              fuse_map_target(20.0, 30.0, False, c, advisory_active=False))

  def test_full_vision_activation_is_untouched_by_it(self):
    assert fuse_map_target(5.0, 30.0, True, 0.0, advisory_active=True) == 5.0

  def test_the_solo_ceiling_still_bounds_it(self):
    """MUTATION: let the advisory bypass MAP_SOLO_MAX_CUT. An advisory raises
    confidence; it does not remove the bound on an uncorroborated cut."""
    v = fuse_map_target(2.0, 30.0, False, 0.0, advisory_active=True)
    assert v >= 30.0 - MAP_SOLO_MAX_CUT - 1e-6

  def test_no_map_request_stays_inactive(self):
    assert fuse_map_target(CAP_INACTIVE, 30.0, False, 0.0, advisory_active=True) == CAP_INACTIVE


class TestSccShm:
  """The minimap's marker comes from the controller, not from the UI redoing
  argmin. These pin the channel's failure mode: no marker, never a stuck one."""

  def test_round_trip(self, tmp_path, monkeypatch):
    monkeypatch.setattr(scc_shm, "SHM_PATH", str(tmp_path / "fp_scc"))
    scc_shm.write_scc_shm(35.123456, -78.987654, 17.9, 1.0, True)
    lat, lon, v, auth, adv = scc_shm.read_scc_shm()
    assert abs(lat - 35.123456) < 1e-5
    assert abs(lon + 78.987654) < 1e-5
    assert abs(v - 17.9) < 1e-2
    assert auth == 1.0 and adv is True

  def test_missing_file_is_inactive(self, tmp_path, monkeypatch):
    monkeypatch.setattr(scc_shm, "SHM_PATH", str(tmp_path / "nope"))
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_garbage_is_inactive(self, tmp_path, monkeypatch):
    p = tmp_path / "fp_scc"
    p.write_text("not,a,valid,line")
    monkeypatch.setattr(scc_shm, "SHM_PATH", str(p))
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_stale_is_inactive_even_with_a_good_payload(self, tmp_path, monkeypatch):
    """MUTATION: drop the freshness check. A wedged plannerd would leave the
    minimap marking a corner it stopped thinking about minutes ago."""
    p = tmp_path / "fp_scc"
    old = time.monotonic() - (scc_shm.STALE_S + 1.0)
    p.write_text(f"35.0,-78.0,17.9,1.0,1,{old:.3f}")
    monkeypatch.setattr(scc_shm, "SHM_PATH", str(p))
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_a_stamp_from_the_future_is_rejected(self, tmp_path, monkeypatch):
    p = tmp_path / "fp_scc"
    p.write_text(f"35.0,-78.0,17.9,1.0,1,{time.monotonic() + 60:.3f}")
    monkeypatch.setattr(scc_shm, "SHM_PATH", str(p))
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_authority_is_clamped(self, tmp_path, monkeypatch):
    p = tmp_path / "fp_scc"
    p.write_text(f"35.0,-78.0,17.9,7.5,0,{time.monotonic():.3f}")
    monkeypatch.setattr(scc_shm, "SHM_PATH", str(p))
    assert scc_shm.read_scc_shm()[3] == 1.0

  def test_writing_to_a_bad_path_never_raises(self, monkeypatch):
    """This runs inside plannerd. A telemetry problem may not touch control."""
    monkeypatch.setattr(scc_shm, "SHM_PATH", "/nonexistent-dir/fp_scc")
    scc_shm.write_scc_shm(1.0, 2.0, 3.0, 1.0, False)


class TestGoverningPointIsPublished:
  def test_the_chosen_point_is_recorded(self):
    """MUTATION: stop recording gov_lat/lon. The UI would have to re-derive
    argmin(v_allowed), and two copies of a selection rule drift."""
    points = route_north([30, 30, 30, 10, 30, 30])
    scc = make(points)
    run(scc, n=200)
    assert scc.is_active
    assert scc.gov_lat != 0.0

  def test_it_clears_when_nothing_constrains(self):
    scc = make([])
    run(scc, n=10)
    assert scc.gov_lat == 0.0 and scc.gov_lon == 0.0
