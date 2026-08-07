"""FunnyPilot v3.6.2 — measuring a corner from the line the minimap draws.

THE TWO FAILURES THAT MATTER, and they are not symmetric:

  A corner measured LOOSE is taken too fast. Nothing downstream can recover
  from it, because the cap is already the number we chose.

  A corner measured TIGHT costs speed, and the learned lateral budget buys it
  back over the next few passes.

  A STRAIGHT ROAD measured as a corner is the one this fork has spent releases
  on — the car braking for a lane merge, for nothing the driver can see.

So the tests below are weighted that way: exactness on clean data, a bounded
loose tail on dirty data, and a hard requirement that noise never produces a
corner at all.

Import-light: stdlib only.
"""
import math
import random

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import road_geometry as RG

M = RG.M_PER_DEG


def arc_route(radius, arc_deg, node_m=2.0, noise=0.0, seed=1,
              before=200.0, after=200.0):
  """A straight, then a constant-radius right-hand bend, then a straight.

  Built as sparse NODES and then densified to ~1 m spacing, because that is
  what mapd publishes: an interpolation of an OSM way, i.e. a polyline that is
  dead straight between nodes and turns all at once at each one. Testing
  against a smooth analytic curve would test an estimator we do not have.
  """
  rnd = random.Random(seed)
  pts = []
  x = -before
  while x < 0:
    pts.append((x, 0.0))
    x += node_m
  n = max(2, int(radius * math.radians(arc_deg) / node_m))
  for i in range(n + 1):
    th = math.radians(arc_deg) * i / n
    pts.append((radius * math.sin(th), radius * (1 - math.cos(th))))
  ex, ey = pts[-1]
  th = math.radians(arc_deg)
  k = 1
  while k * node_m < after:
    pts.append((ex + k * node_m * math.cos(th), ey + k * node_m * math.sin(th)))
    k += 1
  if noise:
    pts = [(px + rnd.gauss(0, noise), py + rnd.gauss(0, noise)) for px, py in pts]

  dense = []
  for i in range(1, len(pts)):
    a, b = pts[i - 1], pts[i]
    seg = math.hypot(b[0] - a[0], b[1] - a[1])
    steps = max(1, int(seg))
    for j in range(steps):
      t = j / steps
      dense.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
  dense.append(pts[-1])
  return [(px / M, py / M) for px, py in dense]


def straight_route(node_m=30.0, noise=0.0, seed=1, length=800.0):
  rnd = random.Random(seed)
  nodes = [(x, rnd.gauss(0, noise) if noise else 0.0)
           for x in range(-200, int(length), int(node_m))]
  dense = []
  for i in range(1, len(nodes)):
    a, b = nodes[i - 1], nodes[i]
    steps = max(1, int(node_m))
    for j in range(steps):
      t = j / steps
      dense.append(((a[0] + (b[0] - a[0]) * t) / M, (a[1] + (b[1] - a[1]) * t) / M))
  return dense


def tightest(points):
  corners, _s = RG.corners_from_route(points, 0.0, 0.0, 0.0)
  return min(corners, key=lambda c: c.radius) if corners else None


# ── the estimator itself ────────────────────────────────────────────────────

class TestExactOnACircle:
  """The property everything else is built on. `kappa = total turn / arc
  length` is the DEFINITION of average curvature, so on a circle it must come
  back exactly, at any point spacing."""

  @pytest.mark.parametrize("radius", [40.0, 100.0, 250.0, 600.0])
  def test_curvature_profile_recovers_the_radius(self, radius):
    # A dense, noise-free circle, measured before any smoothing or trimming.
    # The arc must be several windows long, or the middle third still contains
    # truncated windows and the test measures the truncation, not the circle.
    span = max(8.0 * RG.WINDOW_M, 2.0 * radius)
    pts = [(radius * math.sin(t / radius) / M, radius * (1 - math.cos(t / radius)) / M)
           for t in [i * 1.0 for i in range(int(span))]]
    xy = RG.to_local(pts, 0.0, 0.0, 0.0)
    rs = RG.resample(xy)
    kap = RG.curvature_profile(rs)
    # ignore the truncated windows at either end
    mid = kap[len(kap) // 3: 2 * len(kap) // 3]
    assert mid, "profile too short to have a middle"
    for k in mid:
      assert abs(abs(k) - 1.0 / radius) < 0.05 / radius

  def test_a_straight_line_has_no_curvature(self):
    xy = RG.to_local([(x / M, 0.0) for x in range(400)], 0.0, 0.0, 0.0)
    kap = RG.curvature_profile(RG.resample(xy))
    assert max(abs(k) for k in kap) < 1e-9


class TestPointSpacingDoesNotChangeTheAnswer:
  """mapd's polyline is a staircase: straight between OSM nodes, turning all at
  once at each one. Differencing that aliases the window against the node
  spacing, and because the radius is taken at the TIGHTEST point the aliasing
  always resolves pessimistically. `smooth_polyline` is what removes it."""

  @pytest.mark.parametrize("node_m", [2.0, 15.0, 30.0, 60.0])
  def test_a_100m_bend_reads_as_a_100m_bend(self, node_m):
    c = tightest(arc_route(100.0, 90.0, node_m=node_m))
    assert c is not None
    assert 60.0 <= c.radius <= 115.0

  def test_smoothing_is_what_does_it(self):
    """MUTATION: remove smooth_polyline from the pipeline. Measured at 71 m for
    a 100 m bend with 20 m nodes — a corner permanently 16% slower than it is."""
    pts = arc_route(100.0, 90.0, node_m=20.0)
    xy = RG.to_local(pts, 0.0, 0.0, 0.0)
    rs = RG.resample(xy)
    raw = max(abs(k) for k in RG.curvature_profile(rs))
    sm = max(abs(k) for k in RG.curvature_profile(RG.smooth_polyline(rs)))
    assert 1.0 / raw < 80.0, "the unsmoothed estimator should be badly tight"
    assert 1.0 / sm > 88.0, "smoothing should recover most of the radius"


# ── the guard ───────────────────────────────────────────────────────────────

class TestANoisyStraightRoadIsNotACorner:
  """THE MOST IMPORTANT TEST IN THIS FILE.

  With 2 m of node position error — ordinary for a way traced from imagery — a
  dead straight road produces an apparent 400 m radius, which at the default
  budget asks the car to slow to 60 mph for nothing. No amount of smoothing
  removes it, because the noise and the signal live at the same scale. Total
  turn angle separates them completely: noise turns through a handful of
  degrees, a real corner through tens.
  """

  @pytest.mark.parametrize("node_m", [20.0, 30.0, 60.0, 100.0])
  @pytest.mark.parametrize("noise", [1.0, 2.0])
  def test_no_corners_on_a_straight_road(self, node_m, noise):
    for seed in range(6):
      corners, _ = RG.corners_from_route(
        straight_route(node_m=node_m, noise=noise, seed=seed), 0.0, 0.0, 0.0)
      assert corners == [], f"invented {corners} on a straight road"

  def test_the_turn_gate_is_what_rejects_them(self):
    """MUTATION: set MIN_CORNER_TURN_DEG to 0. Without it the same road that
    passes above produces corners, which is the reported failure mode."""
    pts = straight_route(node_m=30.0, noise=2.0, seed=3)
    xy = RG.to_local(pts, 0.0, 0.0, 0.0)
    sm = RG.smooth_polyline(RG.resample(xy))
    turns = RG.turn_prefix(sm)
    kap = RG.curvature_profile(sm, RG.RESAMPLE_M, RG.WINDOW_M, turns)
    with_gate = RG.find_corners(sm, kap, turns)
    without = RG.find_corners(sm, kap, turns, min_turn_deg=0.0)
    assert with_gate == []
    assert without, "the mutation must actually change something"

  def test_a_real_corner_clears_the_gate_by_a_wide_margin(self):
    for radius, arc in ((40.0, 90.0), (100.0, 90.0), (200.0, 50.0), (400.0, 45.0)):
      c = tightest(arc_route(radius, arc, node_m=30.0, noise=2.0))
      assert c is not None, f"missed a real R={radius} corner"
      assert c.turn_deg > 2 * RG.MIN_CORNER_TURN_DEG


# ── the error budget ────────────────────────────────────────────────────────

class TestTheLooseTailIsBounded:
  """A corner read LOOSE is taken too fast and nothing downstream recovers.
  This pins the tail that `tight_trim()` exists to hold down."""

  @pytest.mark.parametrize("radius,arc", [(40.0, 90.0), (100.0, 90.0),
                                          (200.0, 50.0), (400.0, 45.0)])
  @pytest.mark.parametrize("node_m", [15.0, 30.0, 60.0])
  @pytest.mark.parametrize("noise", [0.0, 2.0])
  def test_never_more_than_20_percent_loose(self, radius, arc, node_m, noise):
    c = tightest(arc_route(radius, arc, node_m=node_m, noise=noise))
    if c is None:
      return          # missing a gentle corner costs speed, not safety
    assert c.radius <= radius * 1.20, f"read {c.radius:.0f} for a {radius:.0f} m bend"

  def test_tight_trim_only_ever_lowers(self):
    """MUTATION: make tight_trim return the radius unchanged, or scale it up.
    It exists to hold the loose tail down; a version that raises anything is
    the opposite of the point."""
    for r in (18.0, 25.0, 50.0, 90.0, 120.0, 400.0):
      assert RG.tight_trim(r) <= r + 1e-9
    assert RG.tight_trim(40.0) < 40.0, "it must actually bite at the tight end"
    assert RG.tight_trim(400.0) == pytest.approx(400.0), "and not at the loose end"

  def test_it_is_continuous(self):
    """A step in the trim is a step in the speed. Walk it and check no
    neighbouring pair jumps."""
    prev = RG.tight_trim(RG.R_MIN_M)
    r = RG.R_MIN_M
    while r < 300.0:
      r += 0.5
      cur = RG.tight_trim(r)
      assert cur - prev < 1.0
      prev = cur


class TestRadiusIsFloored:
  def test_nothing_tighter_than_R_MIN_is_ever_reported(self):
    """A polyline that jinks is not a hairpin. MUTATION: drop the clamp in
    curvature_profile and a single misplaced node asks for a crawl."""
    # a deliberate spike: two nodes 3 m apart at right angles
    pts = [(x / M, 0.0) for x in range(-60, 0)]
    pts += [(0.0, y / M) for y in range(60)]
    corners, _ = RG.corners_from_route(pts, 0.0, 0.0, 0.0)
    for c in corners:
      assert c.radius >= RG.R_MIN_M - 1e-9


# ── corner identity ─────────────────────────────────────────────────────────

class TestCornerIdentity:
  def test_an_s_bend_is_two_corners(self):
    """Same-signed runs only: a left and a right must not merge into one
    record, or the store would learn one budget for two different bends."""
    left = arc_route(120.0, 60.0, node_m=5.0, after=20.0)
    # mirror the second half
    tail = [(lat, -lon) for lat, lon in arc_route(120.0, 60.0, node_m=5.0, before=0.0)]
    shift_lat = left[-1][0]
    shift_lon = left[-1][1]
    combined = left + [(lat + shift_lat, lon + shift_lon) for lat, lon in tail]
    corners, _ = RG.corners_from_route(combined, 0.0, 0.0, 0.0)
    assert len(corners) >= 2
    assert {c.sign for c in corners} == {1, -1}

  def test_the_apex_is_inside_the_corner(self):
    c = tightest(arc_route(100.0, 90.0, node_m=10.0))
    assert c.s_entry <= c.s_apex <= c.s_exit

  def test_distance_is_measured_along_the_road(self):
    """A corner around a bend must read as its distance ALONG THE ROAD, not as
    the chord. The approach envelope is in metres of travel, so a straight-line
    distance would start the slowdown late on exactly the roads that need it.

    Ego sits at the start of the arc, so the apex is reached by driving round
    the bend. Arc exceeds chord by a ratio that grows with how far round the
    apex sits, so the test uses a hairpin, where it is unmistakable — on a
    gentle bend the two are within a couple of percent and the assertion would
    be measuring floating point rather than the property.
    """
    pts = arc_route(80.0, 180.0, node_m=5.0, before=20.0)
    corners, s_ego = RG.corners_from_route(pts, 0.0, 0.0, 0.0)
    assert corners
    c = max(corners, key=lambda c: c.s_apex)
    d = c.s_apex - s_ego
    chord = math.hypot(c.x, c.y)
    assert d > chord * 1.05, f"along-route {d:.0f} m vs chord {chord:.0f} m"


class TestTotality:
  """This runs in plannerd. Nothing it is handed may raise."""

  @pytest.mark.parametrize("bad", [
    None, [], [(0.0, 0.0)], "not a route", [(None, None)] * 5,
    [(float('nan'), 0.0)] * 5, [(1e9, 1e9)] * 5, [(0.0, 0.0)] * 400,
  ])
  def test_garbage_yields_no_corners(self, bad):
    corners, s = RG.corners_from_route(bad, 0.0, 0.0, 0.0)
    assert corners == [] or all(c.radius > 0 for c in corners)
    assert s == s   # not NaN

  def test_resample_terminates_on_duplicate_points(self):
    assert len(RG.resample([(0.0, 0.0)] * 50)) <= RG.MAX_VERTICES

  def test_the_vertex_budget_is_respected(self):
    long_route = [(x / M, 0.0) for x in range(20000)]
    xy = RG.to_local(long_route, 0.0, 0.0, 0.0)
    assert len(RG.resample(xy)) <= RG.MAX_VERTICES
