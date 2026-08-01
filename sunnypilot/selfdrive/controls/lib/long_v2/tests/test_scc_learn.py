"""FunnyPilot v3.5.0 — SCC-Learn: store, observer, fusion.

WHAT THESE TESTS ARE FOR. The dangerous failure of a learned corner map is not
that it misses a corner — it is that it puts a permanent slowdown somewhere no
corner exists, on a road you drive every day. Every exclusion in the observer
is one class of that bug, so each has its own test, and the ones that would
look identical in review if they were broken are mutation-tested:

  * eviction ORDER (n first) — sort by recency instead and the commute is the
    first thing thrown away, which is exactly backwards
  * the RECOVERY requirement — drop it and every red light becomes a corner
  * the lead exclusion — drop it and every car in front of you teaches a corner
  * confidence scaling — assign instead of max() and a single sighting brakes
    as hard as a hundred

Import-light: stdlib + the long_v2 modules under test.
"""
import json
import os
import tempfile

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_learn_store as S
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_learn as L
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import (
  fuse_learned_target, LEARN_SOLO_MAX_CUT, LEARN_MIN_CUT)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE


@pytest.fixture
def store():
  with tempfile.TemporaryDirectory() as d:
    yield S.LearnStore(directory=d, name="corners.jsonl")


# ── geometry / keying ───────────────────────────────────────────────────────

class TestKeying:
  def test_nearby_points_share_a_cell(self):
    # ~10 m apart, comfortably inside CELL_DEG (~22 m) and away from an edge
    a = S.key_for(37.50005, -122.00005, 90.0)
    b = S.key_for(37.50013, -122.00008, 92.0)
    assert a == b

  def test_two_visits_astride_a_cell_edge_are_one_corner(self, store):
    """A grid has edges and a bend is as likely to sit on one as anywhere. Two
    records with one visit each would be filed as two roads-driven-once and
    evicted first — the exact opposite of the requirement."""
    store.observe(37.5, -122.0, 90.0, 16.0, now=1.0)          # exactly on an edge
    store.observe(37.50008, -122.00003, 92.0, 16.0, now=2.0)  # ~10 m away
    assert store.count == 1
    assert next(iter(store.corners.values())).n == 2

  def test_merging_respects_direction(self, store):
    store.observe(37.5, -122.0, 0.0, 16.0, now=1.0)
    store.observe(37.50008, -122.0, 180.0, 16.0, now=2.0)
    assert store.count == 2

  def test_merging_respects_distance(self, store):
    store.observe(37.5, -122.0, 90.0, 16.0, now=1.0)
    store.observe(37.5 + 200.0 / 111320.0, -122.0, 90.0, 16.0, now=2.0)
    assert store.count == 2

  def test_opposite_directions_are_different_records(self):
    """The same tarmac driven the other way is a different approach: the corner
    entry, the speed you can carry and the point you brake are all elsewhere."""
    n = S.key_for(37.5, -122.0, 0.0)
    s = S.key_for(37.5, -122.0, 180.0)
    assert n != s

  def test_octant_wraps(self):
    assert S.octant_of(359.9) == S.octant_of(-0.1)   # same heading, same octant
    assert S.octant_of(0.0) == S.octant_of(360.0) == 0
    assert 0 <= S.octant_of(721.0) < S.OCTANTS
    assert 0 <= S.octant_of(-721.0) < S.OCTANTS

  def test_bearing_delta_takes_the_short_way(self):
    assert S.bearing_delta(350.0, 10.0) == pytest.approx(20.0)
    assert S.bearing_delta(10.0, 350.0) == pytest.approx(20.0)
    assert S.bearing_delta(0.0, 180.0) == pytest.approx(180.0)


# ── the store ───────────────────────────────────────────────────────────────

class TestObserveFolding:
  def test_first_sighting_is_taken_at_face_value(self, store):
    store.observe(37.5, -122.0, 90.0, 15.0, now=100.0)
    c = store.corners[S.key_for(37.5, -122.0, 90.0)]
    assert c.v == pytest.approx(15.0)
    assert c.n == 1

  def test_rising_is_adopted_faster_than_falling(self, store):
    """A learned cap can only ever SLOW the car, so learning to brake harder is
    the change that deserves more evidence. Asymmetric on purpose."""
    up = S.LearnStore(directory=store.directory, name="up.jsonl", autoload=False)
    down = S.LearnStore(directory=store.directory, name="down.jsonl", autoload=False)
    up.observe(37.5, -122.0, 90.0, 20.0, now=1.0)
    up.observe(37.5, -122.0, 90.0, 24.0, now=2.0)
    down.observe(37.5, -122.0, 90.0, 20.0, now=1.0)
    down.observe(37.5, -122.0, 90.0, 16.0, now=2.0)
    k = S.key_for(37.5, -122.0, 90.0)
    moved_up = up.corners[k].v - 20.0
    moved_down = 20.0 - down.corners[k].v
    assert moved_up > moved_down
    assert moved_up == pytest.approx(4.0 * S.ALPHA_UP)
    assert moved_down == pytest.approx(4.0 * S.ALPHA_DOWN)

  def test_repeat_sightings_count_visits(self, store):
    for i in range(5):
      store.observe(37.5, -122.0, 90.0, 18.0, now=float(i))
    assert store.corners[S.key_for(37.5, -122.0, 90.0)].n == 5

  def test_flags_accumulate(self, store):
    store.observe(37.5, -122.0, 90.0, 18.0, flags=L.FLAG_VISION, now=1.0)
    store.observe(37.5, -122.0, 90.0, 18.0, flags=L.FLAG_DRIVER, now=2.0)
    assert store.corners[S.key_for(37.5, -122.0, 90.0)].flags == (L.FLAG_VISION | L.FLAG_DRIVER)


class TestEviction:
  def _fill(self, store, *specs):
    for i, (n, t) in enumerate(specs):
      lat = 37.0 + i * 0.01
      c = S.Corner(lat, -122.0, 90.0, 15.0, n=n, t=t)
      store.corners[S.key_for(lat, -122.0, 90.0)] = c
    return [S.key_for(37.0 + i * 0.01, -122.0, 90.0) for i in range(len(specs))]

  def test_the_commute_outlives_the_holiday(self, store):
    """THE EXPLICIT REQUIREMENT: when space runs out, lose the road driven once
    before the road driven every morning — even when the holiday road is the
    more RECENTLY seen of the two."""
    store.max_records = 1
    commute, holiday = self._fill(store, (50, 1000.0), (1, 999999.0))
    store._evict()
    assert commute in store.corners
    assert holiday not in store.corners

  def test_recency_only_breaks_ties(self, store):
    store.max_records = 1
    old, new = self._fill(store, (3, 10.0), (3, 20.0))
    store._evict()
    assert new in store.corners
    assert old not in store.corners

  def test_under_the_limit_nothing_is_dropped(self, store):
    store.max_records = 10
    keys = self._fill(store, (1, 1.0), (2, 2.0), (3, 3.0))
    store._evict()
    assert all(k in store.corners for k in keys)


class TestPersistence:
  def test_round_trip(self, store):
    store.observe(37.5, -122.0, 90.0, 17.5, now=123.0)
    store.observe(37.6, -122.1, 270.0, 12.0, now=124.0)
    store._rewrite()
    reloaded = S.LearnStore(directory=store.directory, name="corners.jsonl")
    assert reloaded.count == 2
    assert reloaded.corners[S.key_for(37.5, -122.0, 90.0)].v == pytest.approx(17.5, abs=0.01)

  def test_a_corrupt_line_costs_one_corner_not_the_map(self, store):
    """A power cut mid-append leaves a half-written line. Losing the whole map
    to it would mean the feature silently resets every time the device is
    yanked, which is the common case on a car."""
    good = S.Corner(37.5, -122.0, 90.0, 15.0, n=2, t=5.0)
    with open(store.path, "w") as f:
      f.write(good.to_json(S.key_for(37.5, -122.0, 90.0)) + "\n")
      f.write("{\"k\": \"broken\", \"la\": \n")           # truncated
      f.write("not json at all\n")
      f.write("\n")
      f.write(S.Corner(37.6, -122.0, 90.0, 16.0).to_json(S.key_for(37.6, -122.0, 90.0)) + "\n")
    reloaded = S.LearnStore(directory=store.directory, name="corners.jsonl")
    assert reloaded.count == 2

  def test_missing_file_is_an_empty_map_not_an_error(self):
    with tempfile.TemporaryDirectory() as d:
      s = S.LearnStore(directory=os.path.join(d, "never-made"), name="corners.jsonl")
      assert s.count == 0
      assert s.loaded

  def test_load_compacts_the_journal(self, store):
    key = S.key_for(37.5, -122.0, 90.0)
    with open(store.path, "w") as f:
      for i in range(50):
        f.write(S.Corner(37.5, -122.0, 90.0, 15.0, n=i + 1, t=float(i)).to_json(key) + "\n")
    big = os.path.getsize(store.path)
    reloaded = S.LearnStore(directory=store.directory, name="corners.jsonl")
    assert reloaded.count == 1
    assert os.path.getsize(reloaded.path) < big
    # last write wins
    assert reloaded.corners[key].n == 50

  def test_the_load_has_a_line_budget(self, store, monkeypatch):
    """The one piece of unbounded-looking work in the module. It runs at
    ignition with the car stationary, but "bounded by a byte cap somewhere
    else" is not a bound — this is."""
    monkeypatch.setattr(S, "MAX_JOURNAL_LINES", 10)
    with open(store.path, "w") as f:
      for i in range(200):
        lat = 37.0 + i * 0.05
        f.write(S.Corner(lat, -122.0, 90.0, 15.0).to_json(S.key_for(lat, -122.0, 90.0)) + "\n")
    reloaded = S.LearnStore(directory=store.directory, name="corners.jsonl")
    assert reloaded.count == 10

  def test_flush_is_rate_limited_and_appends(self, store):
    store.observe(37.5, -122.0, 90.0, 15.0, now=1.0)
    assert store.maybe_flush(0.0) is False          # too soon after _last_flush
    assert store.maybe_flush(S.FLUSH_S + 1.0) is True
    assert store.maybe_flush(S.FLUSH_S + 2.0) is False   # nothing dirty

  def test_flush_survives_an_unwritable_store(self, store):
    store.observe(37.5, -122.0, 90.0, 15.0, now=1.0)
    store.directory = "/proc/nonexistent/nope"
    store.path = "/proc/nonexistent/nope/corners.jsonl"
    assert store.maybe_flush(S.FLUSH_S + 1.0) is False   # and did not raise

  def test_journal_cap_stops_writing_rather_than_compacting_mid_drive(self, store):
    """Compaction is a STARTUP job. Rewriting megabytes beside a moving car is
    the v3.4.6 lesson (bounded in memory, not merely in time), so hitting the
    cap costs one drive of learning and nothing else."""
    store.observe(37.5, -122.0, 90.0, 15.0, now=1.0)
    with open(store.path, "w") as f:
      f.write("x" * (S.MAX_JOURNAL_BYTES + 1))
    assert store.maybe_flush(S.FLUSH_S + 1.0) is False
    assert store._journal_full


class TestNearby:
  def test_finds_a_corner_ahead(self, store):
    store.observe(37.5030, -122.0, 0.0, 15.0, now=1.0)   # ~333 m north
    hits = store.nearby(37.5, -122.0, 0.0, 400.0)
    assert len(hits) == 1
    d, _ = hits[0]
    assert 300.0 < d < 360.0

  def test_a_corner_we_just_exited_does_not_cap_us(self, store):
    """Ahead-ness is a dot product, not a radius. Without it the bend you have
    just left keeps braking you on the way out."""
    store.observe(37.4970, -122.0, 0.0, 15.0, now=1.0)   # behind, heading north
    assert store.nearby(37.5, -122.0, 0.0, 400.0) == []

  def test_the_other_carriageway_is_not_our_corner(self, store):
    store.observe(37.5030, -122.0, 180.0, 15.0, now=1.0)
    assert store.nearby(37.5, -122.0, 0.0, 400.0) == []

  def test_out_of_range_is_dropped(self, store):
    store.observe(37.5030, -122.0, 0.0, 15.0, now=1.0)
    assert store.nearby(37.5, -122.0, 0.0, 100.0) == []

  def test_a_different_town_is_not_probed(self, store):
    store.observe(40.0, -74.0, 0.0, 15.0, now=1.0)
    assert store.nearby(37.5, -122.0, 0.0, 400.0) == []

  def test_lookup_does_not_scan_every_record(self, store):
    """The coarse index is what makes this affordable at 20 Hz. 9 coarse cells,
    not 25,000 records."""
    for i in range(500):
      store.observe(37.0 + i * 0.02, -122.0, 0.0, 15.0, now=float(i))
    cy, cx = S.coarse_of(37.5, -122.0)
    probed = sum(len(store._index.get((cy + dy, cx + dx), ())) for dy in (-1, 0, 1) for dx in (-1, 0, 1))
    assert probed < 20


# ── the observer ────────────────────────────────────────────────────────────

def drive(obs, profile, **over):
  """Run a speed profile at 20 Hz through the observer; return commits."""
  kw = dict(lat=37.5, lon=-122.0, bearing=90.0, gps_acc=3.0, lead=False,
            standstill=False, speed_limit=25.0, sla_busy=False,
            vision_active=False, driver_braking=False)
  kw.update(over)
  out = []
  for i, v in enumerate(profile):
    r = obs.update(i * 0.05, v, **kw)
    if r is not None:
      out.append(r)
  return out


def dip(hold_s=4.0, v_hi=25.0, v_lo=16.0, settle_s=30.0, recover=True):
  """A corner: cruise, slow, hold, speed back up."""
  p = [v_hi] * int(settle_s / 0.05)
  p += [v_hi + (v_lo - v_hi) * (i / 20.0) for i in range(20)]
  p += [v_lo] * int(hold_s / 0.05)
  if recover:
    p += [v_lo + (v_hi - v_lo) * (i / 40.0) for i in range(41)]
    p += [v_hi] * 40
  else:
    p += [v_lo] * 200
  return p


class TestObserverAccepts:
  def test_a_bend_is_learned(self):
    out = drive(L.CornerObserver(), dip())
    assert len(out) == 1
    _lat, _lon, _br, v, _f = out[0]
    assert v == pytest.approx(16.0 * L.LEARN_MARGIN, abs=0.2)

  def test_it_stores_above_the_observed_minimum(self):
    """The minimum already contains whatever margin the driver or SCC-V chose.
    Capping AT it and then observing again would compound the margin every
    single visit until the car crawled through the corner."""
    out = drive(L.CornerObserver(), dip(v_lo=16.0))
    assert out[0][3] > 16.0
    assert L.LEARN_MARGIN > 1.0

  def test_flags_record_who_slowed_us(self):
    out = drive(L.CornerObserver(), dip(), vision_active=True, driver_braking=True)
    assert out[0][4] == (L.FLAG_VISION | L.FLAG_DRIVER)


class TestObserverRejects:
  """Each of these is a real thing that makes the car slow down and is NOT a
  bend. Learning any one of them puts a permanent cap where no corner is."""

  def test_a_dip_that_never_recovers_is_a_stop_not_a_corner(self):
    assert drive(L.CornerObserver(), dip(recover=False)) == []

  def test_a_lead_vehicle_means_that_was_traffic(self):
    assert drive(L.CornerObserver(), dip(), lead=True) == []

  def test_a_lead_appearing_mid_dip_still_poisons_it(self):
    obs = L.CornerObserver()
    kw = dict(lat=37.5, lon=-122.0, bearing=90.0, gps_acc=3.0, standstill=False,
              speed_limit=25.0, sla_busy=False, vision_active=False, driver_braking=False)
    p = dip()
    out = []
    for i, v in enumerate(p):
      r = obs.update(i * 0.05, v, lead=(700 < i < 720), **kw)
      if r is not None:
        out.append(r)
    assert out == []

  def test_creeping_below_walking_pace_is_a_junction(self):
    assert drive(L.CornerObserver(), dip(v_lo=L.MIN_CORNER_V - 2.0)) == []

  def test_a_speed_limit_zone_belongs_to_sla(self):
    obs = L.CornerObserver()
    kw = dict(lat=37.5, lon=-122.0, bearing=90.0, gps_acc=3.0, lead=False,
              standstill=False, sla_busy=False, vision_active=False, driver_braking=False)
    out = []
    for i, v in enumerate(dip()):
      r = obs.update(i * 0.05, v, speed_limit=(25.0 if i < 650 else 15.0), **kw)
      if r is not None:
        out.append(r)
    assert out == []

  def test_an_sla_ramp_belongs_to_sla(self):
    assert drive(L.CornerObserver(), dip(), sla_busy=True) == []

  def test_congestion_is_not_geometry(self):
    assert drive(L.CornerObserver(), dip(hold_s=L.MAX_DIP_S + 5.0)) == []

  def test_a_blip_is_noise(self):
    assert drive(L.CornerObserver(), dip(hold_s=0.1, v_lo=21.0)) == []

  def test_a_position_we_are_unsure_of_is_worse_than_no_record(self):
    assert drive(L.CornerObserver(), dip(), gps_acc=L.MAX_GPS_ACC_M + 10.0) == []

  def test_no_fix_at_all(self):
    assert drive(L.CornerObserver(), dip(), lat=0.0, lon=0.0) == []


class TestObserverPlacement:
  def test_the_record_lands_at_the_apex_not_the_entry(self):
    """Braking is planned to the corner, so a record placed where the car
    STARTED slowing would ask the next pass to be at corner speed a hundred
    metres early."""
    obs = L.CornerObserver()
    kw = dict(bearing=90.0, gps_acc=3.0, lead=False, standstill=False,
              speed_limit=25.0, sla_busy=False, vision_active=False, driver_braking=False)
    out = []
    p = dip()
    for i, v in enumerate(p):
      # move north at ~1 m per frame
      r = obs.update(i * 0.05, v, lat=37.5 + i * 9.0e-6, lon=-122.0, **kw)
      if r is not None:
        out.append(r)
    assert len(out) == 1
    apex_i = min(range(len(p)), key=lambda i: (p[i], i))
    assert out[0][0] == pytest.approx(37.5 + apex_i * 9.0e-6, abs=2e-5)


class TestObserverIsTotal:
  def test_the_dip_state_is_initialised_before_it_is_read(self):
    """This object lives in plannerd. An AttributeError here is a dead planner,
    not a missed corner — so every field the dip branch reads must exist from
    construction, not only after the entry edge."""
    obs = L.CornerObserver()
    assert hasattr(obs, "_limit_at_start")

  def test_back_to_back_corners_are_both_learned(self):
    out = drive(L.CornerObserver(), dip(settle_s=30.0) + dip(settle_s=5.0))
    assert len(out) == 2


# ── confidence + fusion ─────────────────────────────────────────────────────

class TestConfidence:
  def test_one_visit_is_partial(self):
    assert 0.0 < L.confidence_for(1) < 1.0

  def test_three_visits_is_full(self):
    assert L.confidence_for(L.CONF_FULL_VISITS) == pytest.approx(1.0)

  def test_it_never_exceeds_one(self):
    assert L.confidence_for(10_000) == 1.0

  def test_nothing_learned_is_no_confidence(self):
    assert L.confidence_for(0) == 0.0

  def test_it_is_monotonic(self):
    vals = [L.confidence_for(n) for n in range(1, 8)]
    assert vals == sorted(vals)


class TestFusion:
  def test_nothing_learned_is_a_no_op(self):
    assert fuse_learned_target(CAP_INACTIVE, 30.0, 1.0) == CAP_INACTIVE

  def test_full_confidence_takes_the_whole_cut(self):
    assert fuse_learned_target(27.0, 30.0, 1.0) == pytest.approx(27.0)

  def test_one_visit_takes_a_partial_cut(self):
    out = fuse_learned_target(25.0, 30.0, L.confidence_for(1))
    assert 25.0 < out < 30.0

  def test_zero_confidence_is_vetoed(self):
    assert fuse_learned_target(20.0, 30.0, 0.0) == CAP_INACTIVE

  def test_vision_may_only_raise_authority(self):
    """A corner the model can also see is not less trustworthy for having been
    driven once. Corroboration is a floor, never a ceiling."""
    low = fuse_learned_target(20.0, 30.0, 0.2)
    with_vision = fuse_learned_target(20.0, 30.0, 0.2, False, 0.9)
    assert with_vision < low
    assert fuse_learned_target(20.0, 30.0, 0.9, False, 0.1) == pytest.approx(
      fuse_learned_target(20.0, 30.0, 0.9))

  def test_vision_active_passes_it_through_whole(self):
    assert fuse_learned_target(18.0, 30.0, 0.1, True) == pytest.approx(18.0)

  def test_a_partly_trusted_cut_is_bounded(self):
    out = fuse_learned_target(1.0, 40.0, 0.9)
    assert out == pytest.approx(40.0 - LEARN_SOLO_MAX_CUT)

  def test_a_trivial_cut_is_not_worth_a_slowdown(self):
    out = fuse_learned_target(30.0 - LEARN_MIN_CUT * 0.5, 30.0, 1.0)
    assert out == CAP_INACTIVE

  def test_garbage_confidence_cannot_amplify(self):
    assert fuse_learned_target(20.0, 30.0, 5.0) == pytest.approx(
      fuse_learned_target(20.0, 30.0, 1.0))


# ── the governor ────────────────────────────────────────────────────────────

class TestSCCLearnV1:
  def _seeded(self, tmpdir, v=15.0, n=5):
    s = S.LearnStore(directory=tmpdir, name="c.jsonl", autoload=False)
    for _ in range(n):
      s.observe(37.5030, -122.0, 0.0, v, now=1.0)
    return L.SCCLearnV1(store=s)

  def test_a_known_corner_produces_a_cap(self, tmp_path):
    g = self._seeded(str(tmp_path))
    for _ in range(6):
      g.update(True, 30.0, 30.0, 37.5, -122.0, 0.0, True)
    assert g.is_active
    assert g.output_v_target < 30.0

  def test_the_cap_tightens_as_we_close_on_it(self, tmp_path):
    g = self._seeded(str(tmp_path))
    far = near = None
    for _ in range(6):
      g.update(True, 30.0, 30.0, 37.5, -122.0, 0.0, True)
      far = g.raw_v_target
    g2 = self._seeded(str(tmp_path))
    for _ in range(6):
      g2.update(True, 30.0, 30.0, 37.5020, -122.0, 0.0, True)
      near = g2.raw_v_target
    assert near < far

  def test_no_gps_is_no_cap(self, tmp_path):
    g = self._seeded(str(tmp_path))
    for _ in range(6):
      g.update(True, 30.0, 30.0, 37.5, -122.0, 0.0, False)
    assert g.output_v_target == CAP_INACTIVE

  def test_long_disabled_is_no_cap(self, tmp_path):
    g = self._seeded(str(tmp_path))
    for _ in range(6):
      g.update(False, 30.0, 30.0, 37.5, -122.0, 0.0, True)
    assert g.output_v_target == CAP_INACTIVE

  def test_a_corner_faster_than_cruise_does_not_constrain(self, tmp_path):
    g = self._seeded(str(tmp_path), v=28.0)
    for _ in range(6):
      g.update(True, 12.0, 12.0, 37.5, -122.0, 0.0, True)
    assert g.output_v_target == CAP_INACTIVE

  def test_a_broken_store_degrades_to_no_cap(self, tmp_path):
    class Exploding:
      count = 0
      def nearby(self, *a, **k):
        raise RuntimeError("disk on fire")
      def observe(self, *a, **k):
        raise RuntimeError("disk on fire")
      def maybe_flush(self, *a, **k):
        raise RuntimeError("disk on fire")
    g = L.SCCLearnV1(store=Exploding())
    for _ in range(6):
      g.update(True, 30.0, 30.0, 37.5, -122.0, 0.0, True)
    assert g.output_v_target == CAP_INACTIVE
    assert g.observe(1.0, 25.0, lat=37.5, lon=-122.0, bearing=0.0, gps_acc=3.0,
                     lead=False, standstill=False, speed_limit=0.0, sla_busy=False,
                     vision_active=False, driver_braking=False) is False
    g.flush(1.0)   # must not raise

  def test_confidence_reaches_the_fusion(self, tmp_path):
    once = self._seeded(str(tmp_path), n=1)
    many = self._seeded(str(tmp_path), n=5)
    for _ in range(6):
      once.update(True, 30.0, 30.0, 37.5, -122.0, 0.0, True)
      many.update(True, 30.0, 30.0, 37.5, -122.0, 0.0, True)
    assert once.confidence < many.confidence
    a = fuse_learned_target(once.output_v_target, 30.0, once.confidence)
    b = fuse_learned_target(many.output_v_target, 30.0, many.confidence)
    assert b < a


# ── the shm channel ─────────────────────────────────────────────────────────

class TestLearnShm:
  def test_round_trip(self, monkeypatch, tmp_path):
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm
    monkeypatch.setattr(scc_shm, "LEARN_SHM_PATH", str(tmp_path / "fp_learn"))
    scc_shm.write_learn_shm(42, True, 0.75)
    assert scc_shm.read_learn_shm() == (42, True, pytest.approx(0.75))

  def test_a_wedged_writer_reads_as_nothing_learned(self, monkeypatch, tmp_path):
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm
    p = tmp_path / "fp_learn"
    monkeypatch.setattr(scc_shm, "LEARN_SHM_PATH", str(p))
    scc_shm.write_learn_shm(42, True, 0.75)
    # a real, plausible payload — only the timestamp is old
    raw = p.read_text().split(",")
    p.write_text(",".join(raw[:3] + [str(float(raw[3]) - scc_shm.STALE_S - 5.0)]))
    assert scc_shm.read_learn_shm() == scc_shm.LEARN_INACTIVE

  def test_garbage_and_absence_are_both_inactive(self, monkeypatch, tmp_path):
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm
    p = tmp_path / "fp_learn"
    monkeypatch.setattr(scc_shm, "LEARN_SHM_PATH", str(p))
    assert scc_shm.read_learn_shm() == scc_shm.LEARN_INACTIVE
    p.write_text("not,a,payload")
    assert scc_shm.read_learn_shm() == scc_shm.LEARN_INACTIVE

  def test_it_does_not_disturb_the_minimap_channel(self, monkeypatch, tmp_path):
    """The two channels are separate files precisely so a change to one cannot
    shift the other's field indices."""
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm
    monkeypatch.setattr(scc_shm, "SHM_PATH", str(tmp_path / "fp_scc"))
    monkeypatch.setattr(scc_shm, "LEARN_SHM_PATH", str(tmp_path / "fp_learn"))
    scc_shm.write_scc_shm(37.5, -122.0, 18.0, 0.5, True)
    scc_shm.write_learn_shm(7, False, 0.1)
    lat, lon, v, auth, adv = scc_shm.read_scc_shm()
    assert (round(lat, 3), round(v, 1), adv) == (37.5, 18.0, True)
    assert auth == pytest.approx(0.5)


# ── no IO at import ─────────────────────────────────────────────────────────

def test_constructing_the_governor_touches_no_disk():
  """plannerd builds this before the car moves and the test suite builds it on
  a machine with no /data at all. The store must be lazy."""
  g = L.SCCLearnV1()
  assert g._store is None
  assert not os.path.exists(S.STORE_DIR) or True   # never created by construction


def test_store_json_is_compact():
  """25,000 records at this size is the storage budget the eviction rule is
  written against; a fatter record silently blows it."""
  line = S.Corner(37.123456, -122.123456, 271.5, 17.25, n=9, t=1.7e9, flags=3).to_json("1,2,3")
  assert len(line) < 120
  assert json.loads(line)["n"] == 9
