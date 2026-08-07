"""FunnyPilot v3.6.2 — the learned-corner store: keying, folding, persistence.

WHAT THESE TESTS ARE FOR. The dangerous failure of a learned corner map is not
that it misses a corner — it is that it puts a permanent slowdown somewhere no
corner exists, on a road you drive every day, or that it forgets the roads you
actually drive. The rules that would look identical in review if they were
inverted are mutation-tested:

  * eviction ORDER (visit count first) — sort by recency instead and the
    commute is the first thing thrown away, which is exactly backwards
  * cell-edge merging — without it a bend on a grid boundary is filed as two
    roads-driven-once and evicted before a road driven once on holiday
  * the self-governed raise block — without it the cap sets the speed, the
    speed sets the floor, and the estimate ratchets until the feature stops
    working on precisely the roads it was built for
  * `maybe_flush` making no syscalls — the v3.5.1 rule, pinned on the AST
    because the natural way to write that function is the way that caused the
    bug

Import-light: stdlib + the long_v2 modules under test.
"""
import os
import tempfile

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_speed as CS
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_learn_store as S

CLEAN = 0.0      # severity of a comfortable pass
STRESSED = 1.5   # ...and of one that ran out of grip or steering


@pytest.fixture
def store():
  with tempfile.TemporaryDirectory() as d:
    s = S.LearnStore(directory=d, name="corners_v2.jsonl")
    # v3.5.1: writes happen on a short-lived thread so plannerd's 20 Hz loop
    # never touches /data. Join the startup rewrite BEFORE the test body, or it
    # lands on top of whatever the test writes to the file itself.
    s.join_writes()
    yield s
    s.join_writes()


def see(store, lat=37.5, lon=-122.0, bearing=90.0, radius=100.0,
        a_peak=2.4, severity=CLEAN, **kw):
  return store.observe(lat, lon, bearing, radius, a_peak, severity, **kw)


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
    see(store, now=1.0)                                        # exactly on an edge
    see(store, lat=37.50008, lon=-122.00003, bearing=92.0, now=2.0)  # ~10 m away
    assert store.count == 1
    assert next(iter(store.corners.values())).n == 2

  def test_merging_respects_direction(self, store):
    see(store, bearing=0.0, now=1.0)
    see(store, lat=37.50008, bearing=180.0, now=2.0)
    assert store.count == 2

  def test_merging_respects_distance(self, store):
    see(store, now=1.0)
    see(store, lat=37.5 + 200.0 / 111320.0, now=2.0)
    assert store.count == 2

  def test_opposite_directions_are_different_records(self):
    """The same tarmac driven the other way is a different approach: the corner
    entry, the speed you can carry and the point you brake are all elsewhere."""
    assert S.key_for(37.5, -122.0, 0.0) != S.key_for(37.5, -122.0, 180.0)

  def test_octant_wraps(self):
    assert S.octant_of(359.9) == S.octant_of(-0.1)
    assert S.octant_of(0.0) == S.octant_of(360.0) == 0
    assert 0 <= S.octant_of(721.0) < S.OCTANTS
    assert 0 <= S.octant_of(-721.0) < S.OCTANTS

  def test_bearing_delta_takes_the_short_way(self):
    assert S.bearing_delta(350.0, 10.0) == pytest.approx(20.0)
    assert S.bearing_delta(10.0, 350.0) == pytest.approx(20.0)
    assert S.bearing_delta(0.0, 180.0) == pytest.approx(180.0)


# ── folding an observation in ───────────────────────────────────────────────

class TestObserveFolding:
  def test_a_new_record_starts_at_the_default(self, store):
    see(store, a_peak=0.0, severity=0.0, now=1.0)
    c = store.corners[S.key_for(37.5, -122.0, 90.0)]
    assert c.a_lo == pytest.approx(CS.A_LAT_DEFAULT)
    assert c.a_hi == pytest.approx(CS.A_LAT_MAX)
    assert c.n == 1

  def test_a_clean_fast_pass_raises_the_floor(self, store):
    see(store, a_peak=2.6, severity=CLEAN, now=1.0)
    c = store.corners[S.key_for(37.5, -122.0, 90.0)]
    assert c.a_lo == pytest.approx(2.6)

  def test_a_stressed_pass_lowers_the_ceiling(self, store):
    see(store, a_peak=2.4, severity=2.0, now=1.0)
    c = store.corners[S.key_for(37.5, -122.0, 90.0)]
    assert c.a_hi == pytest.approx(1.2, abs=0.02)

  def test_a_pass_we_governed_cannot_raise_the_floor(self, store):
    """THE SELF-REINFORCEMENT LOOP. If the cap sets the speed, and the speed
    sets a_peak, and a_peak raises the floor, the estimate ratchets up a few
    percent per visit until the feature quietly stops working on exactly the
    roads it is meant for. The visit still counts; only the raise is refused."""
    k = see(store, a_peak=1.6, severity=CLEAN, now=1.0)
    for i in range(10):
      see(store, a_peak=3.0, severity=CLEAN, now=float(i + 2), allow_raise=False)
    assert store.corners[k].a_lo <= CS.A_LAT_DEFAULT + 1e-9
    assert store.corners[k].n == 11

  def test_a_governed_pass_can_still_lower_the_ceiling(self, store):
    """MUTATION: block the ceiling too. A pass WE held back that still
    oscillated is real evidence in the safe direction — refusing it would mean
    the one case where we are demonstrably wrong is the one we never learn
    from."""
    k = see(store, a_peak=2.2, severity=CLEAN, now=1.0)
    before = store.corners[k].a_hi
    see(store, a_peak=2.0, severity=2.0, now=2.0, allow_raise=False)
    assert store.corners[k].a_hi < before

  def test_repeat_sightings_count_visits(self, store):
    for i in range(5):
      see(store, now=float(i))
    assert store.corners[S.key_for(37.5, -122.0, 90.0)].n == 5

  def test_the_radius_is_recorded(self, store):
    k = see(store, radius=143.0, now=1.0)
    assert store.corners[k].r == pytest.approx(143.0)

  def test_flags_accumulate(self, store):
    k = see(store, flags=1, now=1.0)
    see(store, flags=2, now=2.0)
    assert store.corners[k].flags == 3


class TestEviction:
  def _fill(self, store, *specs):
    for i, (n, t) in enumerate(specs):
      lat = 37.0 + i * 0.01
      store.corners[S.key_for(lat, -122.0, 90.0)] = S.Corner(
        lat, -122.0, 90.0, 1.8, 3.0, 100.0, n=n, t=t)
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
    assert new in store.corners and old not in store.corners

  def test_under_the_limit_nothing_is_dropped(self, store):
    store.max_records = 10
    keys = self._fill(store, (1, 1.0), (2, 2.0), (3, 3.0))
    store._evict()
    assert all(k in store.corners for k in keys)


class TestPersistence:
  def test_round_trip(self, store):
    see(store, a_peak=2.6, severity=CLEAN, radius=88.0, now=123.0)
    see(store, lat=37.6, lon=-122.1, bearing=270.0, a_peak=2.2, severity=2.0, now=124.0)
    store._rewrite()
    store.join_writes()
    reloaded = S.LearnStore(directory=store.directory, name="corners_v2.jsonl")
    reloaded.join_writes()
    assert reloaded.count == 2
    c = reloaded.corners[S.key_for(37.5, -122.0, 90.0)]
    assert c.a_lo == pytest.approx(2.6, abs=0.01)
    assert c.r == pytest.approx(88.0, abs=0.1)

  def test_an_old_format_record_is_skipped_not_reinterpreted(self, store):
    """v3.6.2 changed what a record MEANS: the old `v` was a speed in m/s, the
    new `alo`/`ahi` are accelerations. A converted record would be
    indistinguishable from a measured one while being wrong by a factor of
    eight, so an old line must be dropped, not adapted. The store also writes
    to a new filename so this should never arise — this is the backstop."""
    with open(store.path, "w") as f:
      f.write('{"k":"1,2,3","la":37.5,"lo":-122.0,"br":90.0,"v":16.0,"n":9,"t":5.0,"f":0}\n')
      f.write(S.Corner(37.6, -122.0, 90.0, 2.0, 2.5, 100.0).to_json(
        S.key_for(37.6, -122.0, 90.0)) + "\n")
    reloaded = S.LearnStore(directory=store.directory, name="corners_v2.jsonl")
    reloaded.join_writes()
    assert reloaded.count == 1

  def test_a_corrupt_line_costs_one_corner_not_the_map(self, store):
    """A power cut mid-append leaves a half-written line. Losing the whole map
    to it would mean the feature silently resets every time the device is
    yanked, which is the common case on a car."""
    good = S.Corner(37.5, -122.0, 90.0, 1.8, 2.4, 100.0, n=2, t=5.0)
    with open(store.path, "w") as f:
      f.write(good.to_json(S.key_for(37.5, -122.0, 90.0)) + "\n")
      f.write("{\"k\": \"broken\", \"la\": \n")
      f.write("not json at all\n\n")
      f.write(S.Corner(37.6, -122.0, 90.0, 1.8, 2.4, 100.0).to_json(
        S.key_for(37.6, -122.0, 90.0)) + "\n")
    reloaded = S.LearnStore(directory=store.directory, name="corners_v2.jsonl")
    reloaded.join_writes()
    assert reloaded.count == 2

  def test_missing_file_is_an_empty_map_not_an_error(self):
    with tempfile.TemporaryDirectory() as d:
      s = S.LearnStore(directory=os.path.join(d, "never-made"), name="corners_v2.jsonl")
      s.join_writes()
      assert s.count == 0 and s.loaded

  def test_load_compacts_the_journal(self, store):
    key = S.key_for(37.5, -122.0, 90.0)
    with open(store.path, "w") as f:
      for i in range(50):
        f.write(S.Corner(37.5, -122.0, 90.0, 1.8, 2.4, 100.0,
                         n=i + 1, t=float(i)).to_json(key) + "\n")
    big = os.path.getsize(store.path)
    reloaded = S.LearnStore(directory=store.directory, name="corners_v2.jsonl")
    reloaded.join_writes()
    assert reloaded.count == 1
    assert os.path.getsize(reloaded.path) < big
    assert reloaded.corners[key].n == 50     # last write wins

  def test_the_load_has_a_line_budget(self, store, monkeypatch):
    """The one piece of unbounded-looking work in the module. It runs at
    ignition with the car stationary, but "bounded by a byte cap somewhere
    else" is not a bound — this is."""
    monkeypatch.setattr(S, "MAX_JOURNAL_LINES", 10)
    with open(store.path, "w") as f:
      for i in range(200):
        lat = 37.0 + i * 0.05
        f.write(S.Corner(lat, -122.0, 90.0, 1.8, 2.4, 100.0).to_json(
          S.key_for(lat, -122.0, 90.0)) + "\n")
    reloaded = S.LearnStore(directory=store.directory, name="corners_v2.jsonl")
    reloaded.join_writes()
    assert reloaded.count == 10

  def test_flush_is_rate_limited_and_appends(self, store):
    store.join_writes()
    see(store, now=1.0)
    assert store.maybe_flush(0.0) is False
    assert store.maybe_flush(S.FLUSH_S + 1.0) is True
    store.join_writes()
    assert store.maybe_flush(S.FLUSH_S * 2 + 2.0) is False

  def test_the_flush_path_makes_no_syscalls(self, store):
    """THE v3.5.1 RULE: plannerd's loop may not touch /data at all. A blocked
    append is 0.5 s of missed longitudinalPlan, which selfdrived reads as
    `commIssue` — a full-screen 'TAKE CONTROL IMMEDIATELY' for a car that is
    driving perfectly. Pinned on the AST because it is the whole fix."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(S.LearnStore.maybe_flush)))
    banned = {"open", "getsize", "statvfs", "makedirs", "stat", "_have_space", "_write_blocking"}
    for node in ast.walk(tree):
      if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        assert name not in banned, f"maybe_flush must not call {name} on the planner thread"

  def test_flush_survives_an_unwritable_store(self, store):
    store.join_writes()
    see(store, now=1.0)
    store.directory = "/proc/nonexistent/nope"
    store.path = "/proc/nonexistent/nope/corners_v2.jsonl"
    store.maybe_flush(S.FLUSH_S + 1.0)   # must not raise, here or on the thread
    store.join_writes()
    assert not os.path.exists(store.path)

  def test_journal_cap_stops_writing_rather_than_compacting_mid_drive(self, store):
    """Compaction is a STARTUP job. Rewriting megabytes beside a moving car is
    the v3.4.6 lesson (bounded in memory, not merely in time), so hitting the
    cap costs one drive of learning and nothing else."""
    store.join_writes()
    see(store, now=1.0)
    store._journal_bytes = S.MAX_JOURNAL_BYTES + 1
    assert store.maybe_flush(S.FLUSH_S + 1.0) is False
    assert store._journal_full


class TestNearby:
  def test_finds_a_corner_ahead(self, store):
    see(store, lat=37.5030, bearing=0.0, now=1.0)        # ~333 m north
    hits = store.nearby(37.5, -122.0, 0.0, 400.0)
    assert len(hits) == 1
    assert 300.0 < hits[0][0] < 360.0

  def test_a_corner_we_just_exited_does_not_cap_us(self, store):
    """Ahead-ness is a dot product, not a radius. Without it the bend you have
    just left keeps braking you on the way out."""
    see(store, lat=37.4970, bearing=0.0, now=1.0)
    assert store.nearby(37.5, -122.0, 0.0, 400.0) == []

  def test_the_other_carriageway_is_not_our_corner(self, store):
    see(store, lat=37.5030, bearing=180.0, now=1.0)
    assert store.nearby(37.5, -122.0, 0.0, 400.0) == []

  def test_out_of_range_is_dropped(self, store):
    see(store, lat=37.5030, bearing=0.0, now=1.0)
    assert store.nearby(37.5, -122.0, 0.0, 100.0) == []

  def test_a_different_town_is_not_probed(self, store):
    see(store, lat=40.0, lon=-74.0, bearing=0.0, now=1.0)
    assert store.nearby(37.5, -122.0, 0.0, 400.0) == []

  def test_lookup_does_not_scan_every_record(self, store):
    """The coarse index is what makes this affordable at 20 Hz: 9 coarse cells,
    not 25,000 records."""
    for i in range(500):
      see(store, lat=37.0 + i * 0.02, bearing=0.0, now=float(i))
    cy, cx = S.coarse_of(37.5, -122.0)
    probed = sum(len(store._index.get((cy + dy, cx + dx), ()))
                 for dy in (-1, 0, 1) for dx in (-1, 0, 1))
    assert probed < 20


def test_store_json_is_compact():
  """25,000 records have to fit somewhere reasonable on a device whose disk
  pressure is already a documented failure mode."""
  line = S.Corner(37.123456, -122.654321, 91.4, 1.83, 2.44, 118.0,
                  n=7, t=1.7e9, flags=3).to_json("1234,5678,2")
  assert len(line) < 130


class TestLookingUpACornerAtItsOwnPosition:
  """THE BUG THIS EXISTS FOR, found by the end-to-end test and by nothing else.

  `nearby()` filters to corners AHEAD by the dot product of the heading with
  the offset. SCC-M v2 looks a corner up at the corner's OWN position, where
  that offset is zero and the dot product is exactly zero — so the ahead test
  rejected the record it was looking for, nothing learned was ever used, and
  every unit test in this file still passed.
  """

  def test_ahead_only_rejects_a_corner_at_the_query_point(self, store):
    see(store, lat=37.5, lon=-122.0, bearing=90.0, now=1.0)
    assert store.nearby(37.5, -122.0, 90.0, 45.0, 55.0) == []

  def test_ahead_only_false_finds_it(self, store):
    see(store, lat=37.5, lon=-122.0, bearing=90.0, now=1.0)
    hits = store.nearby(37.5, -122.0, 90.0, 45.0, 55.0, ahead_only=False)
    assert len(hits) == 1
    assert hits[0][0] == pytest.approx(0.0, abs=1.0)

  def test_it_still_respects_distance_and_heading(self, store):
    """Relaxing ahead-ness must not relax anything else, or a corner on the
    other carriageway would answer for ours."""
    see(store, lat=37.5, lon=-122.0, bearing=90.0, now=1.0)
    assert store.nearby(37.5, -122.0, 270.0, 45.0, 55.0, ahead_only=False) == []
    assert store.nearby(37.5 + 500.0 / 111320.0, -122.0, 90.0, 45.0, 55.0,
                        ahead_only=False) == []
