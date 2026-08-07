"""FunnyPilot v3.6.2 — SCC-M v2 end to end: measure, cap, drive, learn, repeat.

The pieces are tested in isolation elsewhere (road_geometry, corner_speed,
corner_effort, scc_learn_store). What can only be tested here is that they are
wired the right way round, and four properties in particular:

  1. THE SET SPEED IS A HARD CEILING. Requirement 7, and the one property that
     must hold locally rather than as a consequence of what the governor does
     with the value.
  2. LEARNING RUNS WITH NOTHING ENGAGED. Requirement 4. A pass driven by a
     human, with the toggle off, must still populate the store.
  3. A CORNER IS NEVER LEARNED FROM THE PASS THAT IS CAPPING IT UPWARDS.
     Otherwise the estimate ratchets and the feature stops working on the
     roads it is for.
  4. IT NEVER RAISES. Every failure path in the controller must degrade to
     CAP_INACTIVE, which makes the governor's min() a no-op.

Import-light: stdlib + the long_v2 modules under test.
"""
import math
import tempfile

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_speed as CS
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_learn_store as LS
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import road_geometry as RG
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_map_v2 as M
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE

M_PER_DEG = 111320.0


class NoParams:
  def get_bool(self, key):
    return True


def bend_route(radius=120.0, arc_deg=90.0, lead_in=250.0, node_m=4.0):
  """A straight run-in, then a right-hand bend, as (lat, lon) at ~1 m spacing.

  Ego sits at the origin heading north (bearing 0), so `lat` is forward.
  """
  pts = []
  x = -20.0
  while x < lead_in:
    pts.append((x, 0.0))
    x += node_m
  n = max(2, int(radius * math.radians(arc_deg) / node_m))
  for i in range(n + 1):
    th = math.radians(arc_deg) * i / n
    pts.append((lead_in + radius * math.sin(th), radius * (1 - math.cos(th))))
  dense = []
  for i in range(1, len(pts)):
    a, b = pts[i - 1], pts[i]
    seg = math.hypot(b[0] - a[0], b[1] - a[1])
    for j in range(max(1, int(seg))):
      t = j / max(1, int(seg))
      dense.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
  return [(px / M_PER_DEG, py / M_PER_DEG) for px, py in dense]


def make(points=None, store=None):
  return M.SCCMapV2(params=NoParams(),
                    route_reader=lambda: (points if points is not None else []),
                    store=store)


def run(scc, v_ego=25.0, v_cruise=25.0, n=1, long_enabled=True,
        lat=0.0, lon=0.0, bearing=0.0, gps_ok=True):
  for _ in range(n):
    scc._geom_at = 0.0     # the 0.5 s throttle would otherwise dominate a test
    scc.update(long_enabled, v_ego, 0.0, v_cruise, lat, lon, bearing, gps_ok)


@pytest.fixture
def store():
  with tempfile.TemporaryDirectory() as d:
    s = LS.LearnStore(directory=d, name="corners_v2.jsonl")
    s.join_writes()
    yield s
    s.join_writes()


# ── measuring and capping ───────────────────────────────────────────────────

class TestTheCapComesFromGeometry:
  def test_no_route_means_no_constraint(self):
    scc = make([])
    run(scc, n=10)
    assert not scc.is_active
    assert scc.output_v_target == CAP_INACTIVE

  def test_a_straight_road_means_no_constraint(self):
    straight = [(x / M_PER_DEG, 0.0) for x in range(-20, 400)]
    scc = make(straight)
    run(scc, n=10)
    assert not scc.is_active

  def test_a_bend_ahead_constrains(self):
    scc = make(bend_route(radius=100.0))
    run(scc, v_ego=28.0, v_cruise=28.0, n=20)
    assert scc.is_active
    assert scc.output_v_target < 28.0
    assert scc.corner_radius_m > 0.0

  def test_the_speed_is_sqrt_a_r_at_the_default_budget(self):
    """The chosen speed must be APPARENT, not emergent. With nothing learned it
    is exactly sqrt(A_LAT_DEFAULT * R) for the radius that was measured — one
    multiplication from a number you can read off the corner."""
    scc = make(bend_route(radius=100.0))
    run(scc, v_ego=28.0, v_cruise=28.0, n=5)
    c = min(scc.corners, key=lambda c: c.v_target)
    assert c.a_lat == pytest.approx(CS.A_LAT_DEFAULT)
    assert c.v_target == pytest.approx(CS.speed_for(c.radius, CS.A_LAT_DEFAULT))

  def test_a_tighter_bend_gets_a_lower_speed(self):
    speeds = []
    for r in (60.0, 120.0, 300.0):
      # lead_in kept short so even the widest bend's apex is inside the 400 m
      # lookahead; otherwise the test measures MAX_LOOKAHEAD_M, not the radius
      scc = make(bend_route(radius=r, lead_in=60.0))
      run(scc, v_ego=30.0, v_cruise=30.0, n=5)
      speeds.append(min(c.v_target for c in scc.corners))
    assert speeds[0] < speeds[1] < speeds[2]

  def test_the_cap_tightens_as_the_corner_closes(self):
    far = make(bend_route(radius=100.0, lead_in=380.0))
    near = make(bend_route(radius=100.0, lead_in=120.0))
    run(far, v_ego=28.0, v_cruise=28.0, n=3)
    run(near, v_ego=28.0, v_cruise=28.0, n=3)
    assert near.raw_v_target < far.raw_v_target

  def test_mapd_velocities_are_never_read(self):
    """The premise of the whole rewrite. MUTATION: read p["velocity"] again —
    the route reader would then need it, and this route has no such key."""
    import ast
    import inspect
    src = inspect.getsource(M)
    tree = ast.parse(src)
    for node in ast.walk(tree):
      if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        assert node.slice.value != "velocity", "SCC-M v2 must not read mapd's speeds"


class TestTheSetSpeedIsACeiling:
  """REQUIREMENT 7. The governor's min() already enforces it, but a cap that
  can be read as 'SCC-M wants 30' when the driver asked for 20 is a value
  waiting to be misused — and CurveSpeedCap's release ceiling deliberately
  sits a little ABOVE cruise, so the guard is not hypothetical."""

  def test_the_output_never_exceeds_the_set_speed(self):
    scc = make(bend_route(radius=100.0))
    for v_cruise in (8.0, 12.0, 20.0, 30.0):
      scc._cap.reset()
      for _ in range(60):
        run(scc, v_ego=v_cruise, v_cruise=v_cruise, n=1)
        if scc.is_active:
          assert scc.output_v_target <= v_cruise + 1e-9

  def test_a_cap_above_cruise_is_clamped(self):
    """MUTATION: remove `min(..., v_cruise)` from update(). IT SURVIVES THE
    TEST ABOVE, and that is the finding: CurveSpeedCap resets itself once the
    release reaches `v_cruise - RELEASE_DONE_MARGIN`, so it never actually
    emits a value over cruise and the clamp is unreachable through the normal
    path. It is a BACKSTOP against `curve_cap.py` changing — that file is
    SHARED WITH SCC-V and its release ceiling is deliberately written as
    `v_cruise + 1.0` — so the only honest way to test it is to hand SCC-M v2
    the state the backstop exists for."""
    # a near bend, so the envelope is well inside ACTIVATE_MARGIN at this speed
    scc = make(bend_route(radius=80.0, lead_in=60.0))
    run(scc, v_ego=25.0, v_cruise=25.0, n=20)
    assert scc.is_active, "the fixture must reach the clamped branch"
    scc._cap.value = 40.0          # what a future curve_cap change could emit
    run(scc, v_ego=25.0, v_cruise=25.0, n=1)
    assert scc.output_v_target <= 25.0 + 1e-9

  def test_a_corner_faster_than_cruise_does_not_constrain(self):
    """MUTATION: drop the `v_target >= v_cruise - 0.5` skip. A gentle sweeper
    that permits 30 m/s must not become a constraint at a 12 m/s set speed —
    it would report SCC-M as the plan source for no reason."""
    scc = make(bend_route(radius=600.0, arc_deg=40.0))
    run(scc, v_ego=12.0, v_cruise=12.0, n=20)
    assert not scc.is_active

  def test_it_holds_while_the_cap_is_releasing(self):
    scc = make(bend_route(radius=80.0, lead_in=60.0))
    run(scc, v_ego=25.0, v_cruise=25.0, n=30)
    assert scc.is_active
    scc._read_route = list          # corner passed
    seen_release = False
    for _ in range(60):
      run(scc, v_ego=25.0, v_cruise=25.0, n=1)
      if scc.is_active:
        seen_release = True
        assert scc.output_v_target <= 25.0 + 1e-9
    assert seen_release, "the release path must actually have been exercised"


# ── learning ────────────────────────────────────────────────────────────────

def traverse(scc, seconds=6.0, dt=0.01, v=18.0, curvature=0.012,
             angle=lambda t: 25.0, torque=0.0, lat_active=False,
             saturated=False, eps_limited=False, t0=100.0):
  """Drive through whatever corner SCC-M v2 currently thinks we are inside."""
  n = int(seconds / dt)
  for i in range(n):
    scc.observe_frame(t0 + i * dt, v, curvature, angle(i * dt), torque,
                      lat_active, saturated, eps_limited, False, False, 3.0)


def leave(scc):
  """The next ordinary frame after the corner is behind us.

  It has to be a NEXT frame, not a jump in time: a gap in the sample stream
  abandons the pass rather than committing it, because a traversal that was not
  watched continuously is not a measurement of one.
  """
  scc.corners = []
  scc.observe_frame(scc._last_sample_t + 0.01, 18.0, 0.0, 0.0, 0.0,
                    False, False, False, False, False, 3.0)


def inside_a_corner(store, radius=100.0):
  """A controller placed with a corner directly on top of the car."""
  scc = make(bend_route(radius=radius, lead_in=30.0), store=store)
  run(scc, v_ego=18.0, v_cruise=25.0, n=1)
  assert scc.corners, "the fixture must actually find a corner"
  # walk the car onto the corner
  for c in scc.corners:
    c.distance = 0.0
  return scc


class TestLearningRunsRegardlessOfEngagement:
  """REQUIREMENT 4. The v3.5.0 observer needed a SPEED DIP, which a driver
  taking a bend briskly by hand does not produce. This one is told where the
  corner is by the geometry and measures lateral acceleration, so a pass with
  nothing engaged is a first-class observation."""

  def test_a_hand_driven_pass_is_recorded(self, store):
    scc = inside_a_corner(store)
    traverse(scc, lat_active=False)
    leave(scc)
    assert store.count == 1

  def test_a_clean_brisk_pass_raises_the_budget(self, store):
    scc = inside_a_corner(store)
    # 18 m/s at curvature 0.012 -> 3.9 m/s^2, smooth steering
    traverse(scc, v=18.0, curvature=0.012, lat_active=False)
    leave(scc)
    c = next(iter(store.corners.values()))
    assert c.a_lo > CS.A_LAT_DEFAULT

  def test_driving_far_too_fast_teaches_the_ceiling(self, store):
    """The case the requirement names explicitly: driven far too fast with
    nothing engaged, the signals show HOW FAR past the limits we were, and the
    stored budget lands near what the corner actually supports rather than near
    what was just done."""
    scc = inside_a_corner(store)
    traverse(scc, v=22.0, curvature=0.012,
             angle=lambda t: 25.0 + 5.0 * math.sin(2 * math.pi * 3.0 * t))
    leave(scc)
    c = next(iter(store.corners.values()))
    a_done = 22.0 ** 2 * 0.012
    assert c.a_hi < a_done / 1.5, f"pulled {a_done:.1f}, learned ceiling {c.a_hi:.1f}"

  def test_a_blocked_pass_is_discarded(self, store):
    """A lane change through a bend is not a measurement of the bend."""
    scc = inside_a_corner(store)
    n = int(6.0 / 0.01)
    for i in range(n):
      scc.observe_frame(100.0 + i * 0.01, 18.0, 0.012, 25.0, 0.0, False, False,
                        False, blinker=True, standstill=False, gps_acc=3.0)
    leave(scc)
    assert store.count == 0

  def test_a_pass_with_bad_gps_is_never_keyed(self, store):
    """A record keyed on a position we are unsure of is worse than no record:
    it caps the car somewhere a corner is not."""
    scc = inside_a_corner(store)
    n = int(6.0 / 0.01)
    for i in range(n):
      scc.observe_frame(100.0 + i * 0.01, 18.0, 0.012, 25.0, 0.0, False, False,
                        False, False, False, gps_acc=M.MAX_GPS_ACC_M + 10.0)
    leave(scc)
    assert store.count == 0


class TestLearningFeedsBackIn:
  def test_a_learned_budget_changes_the_speed(self, store):
    scc = inside_a_corner(store)
    traverse(scc, v=18.0, curvature=0.012)
    leave(scc)
    assert store.count == 1

    # the SAME road, so the lookup lands on the record just written; a
    # different lead-in would put the corner elsewhere and the test would
    # silently be comparing two unvisited corners
    fresh = make(bend_route(radius=100.0, lead_in=30.0), store=store)
    run(fresh, v_ego=28.0, v_cruise=28.0, n=2)
    plain = make(bend_route(radius=100.0, lead_in=30.0))
    run(plain, v_ego=28.0, v_cruise=28.0, n=2)
    assert fresh.corners and plain.corners
    assert min(c.v_target for c in fresh.corners) > min(c.v_target for c in plain.corners)
    assert fresh.gov_confidence > 0.0

  def test_confidence_is_published_for_the_fusion(self):
    """An unvisited corner must report zero confidence, or scc_fusion would
    let it bypass the vision veto and the junction-jog protection would be
    gone. MUTATION: default gov_confidence to anything above zero."""
    scc = make(bend_route(radius=100.0))
    run(scc, v_ego=28.0, v_cruise=28.0, n=5)
    assert scc.is_active
    assert scc.gov_confidence == 0.0

  def test_a_pass_we_governed_cannot_raise_the_budget(self, store):
    """THE RATCHET. The cap sets the speed, the speed sets the measured peak,
    the peak raises the floor, and next time the cap is higher. Blocked at the
    store, and pinned here because the wiring is what decides it."""
    scc = inside_a_corner(store)
    scc.is_active = True
    traverse(scc, v=22.0, curvature=0.012)
    leave(scc)
    c = next(iter(store.corners.values()))
    assert c.a_lo <= CS.A_LAT_DEFAULT + 1e-9
    assert c.n == 1, "the visit must still count"


# ── totality ────────────────────────────────────────────────────────────────

class TestItNeverRaises:
  def test_a_route_reader_that_throws(self):
    def boom():
      raise RuntimeError("mapd fell over")
    scc = M.SCCMapV2(params=NoParams(), route_reader=boom)
    run(scc, n=5)
    assert scc.output_v_target == CAP_INACTIVE

  def test_a_store_that_throws(self):
    class BadStore:
      count = 0
      def nearby(self, *a, **k):
        raise RuntimeError("disk gone")
      def observe(self, *a, **k):
        raise RuntimeError("disk gone")
      def maybe_flush(self, *a, **k):
        raise RuntimeError("disk gone")
    scc = make(bend_route(), store=BadStore())
    run(scc, v_ego=28.0, v_cruise=28.0, n=5)
    scc.flush(1.0)
    assert scc.output_v_target < CAP_INACTIVE or scc.output_v_target == CAP_INACTIVE

  def test_garbage_frames_are_survivable(self):
    scc = make(bend_route())
    for bad in (float('nan'), float('inf'), -1.0, None):
      scc.observe_frame(1.0, bad, bad, bad, bad, True, False, False, False, False, 3.0)
    run(scc, n=2)

  def test_no_gps_means_no_corners(self):
    scc = make(bend_route())
    run(scc, v_ego=28.0, v_cruise=28.0, n=5, gps_ok=False)
    assert scc.corners == []
    assert not scc.is_active

  def test_the_toggle_stops_the_cap_but_not_the_geometry(self):
    """Turning SCC-M off should stop the car slowing down, not stop it
    noticing things — the store keeps building and is there the moment the
    toggle goes back on."""
    class Off:
      def get_bool(self, key):
        return False
    scc = M.SCCMapV2(params=Off(), route_reader=lambda: bend_route())
    run(scc, v_ego=28.0, v_cruise=28.0, n=5)
    assert not scc.is_active
    assert scc.output_v_target == CAP_INACTIVE
    assert scc.corners, "the corner list feeds learning and must survive the toggle"

  def test_constructing_it_touches_no_disk(self):
    """The store is lazy. plannerd is onroad-only, but this class is also
    imported by tests and by anything that imports the planner."""
    scc = M.SCCMapV2(params=NoParams(), route_reader=list)
    assert scc._store is None


def s_bend_route(radius=90.0, arc_deg=70.0, between_m=60.0, lead_in=250.0, node_m=4.0):
  """Straight -> right bend -> STRAIGHT -> left bend. The shape the request
  names: the car must not add throttle in the middle straight."""
  pts = []
  x = -20.0
  while x < lead_in:
    pts.append((x, 0.0))
    x += node_m
  n = max(2, int(radius * math.radians(arc_deg) / node_m))
  for i in range(n + 1):
    th = math.radians(arc_deg) * i / n
    pts.append((lead_in + radius * math.sin(th), radius * (1 - math.cos(th))))
  hx, hy = pts[-1]
  th = math.radians(arc_deg)
  k = 1
  while k * node_m < between_m:
    pts.append((hx + k * node_m * math.cos(th), hy + k * node_m * math.sin(th)))
    k += 1
  bx, by = pts[-1]
  for i in range(1, n + 1):
    a = math.radians(arc_deg) * i / n
    lx, ly = radius * math.sin(a), -radius * (1 - math.cos(a))
    pts.append((bx + lx * math.cos(th) - ly * math.sin(th),
                by + lx * math.sin(th) + ly * math.cos(th)))
  ex, ey = pts[-1]
  for k in range(1, 60):
    pts.append((ex + k * node_m, ey))
  dense = []
  for i in range(1, len(pts)):
    a, b = pts[i - 1], pts[i]
    seg = math.hypot(b[0] - a[0], b[1] - a[1])
    for j in range(max(1, int(seg))):
      t = j / max(1, int(seg))
      dense.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
  return [(px / M_PER_DEG, py / M_PER_DEG) for px, py in dense]


def at(scc, route, s_idx, v_ego, v_cruise):
  """Place the car at a point on the route and take one frame."""
  ex, ey = route[s_idx]
  nx, ny = route[min(s_idx + 3, len(route) - 1)]
  brg = math.degrees(math.atan2(ny - ey, nx - ex))
  rel = [(px - ex, py - ey) for px, py in route]
  scc._read_route = lambda rel=rel: rel
  scc._geom_at = 0.0
  scc.update(True, v_ego, 0.0, v_cruise, 0.0, 0.0, brg, True)


class TestTheForwardHorizonIsMeasuredFromTheCar:
  """MAX_POINTS used to truncate the input array from its HEAD. mapd's array
  can contain road already driven, and every point of it spent the budget — so
  the FORWARD horizon shrank by however much of the past mapd happened to still
  be publishing, and a corner silently dropped out of range. A corner that is
  not there is indistinguishable from a road with no corner on it."""

  def test_a_corner_ahead_survives_a_long_tail_behind(self):
    route = bend_route(radius=100.0, lead_in=120.0)
    # 900 points of already-driven road prepended, as mapd may still publish
    behind = [(-(x + 1) / M_PER_DEG, 0.0) for x in range(900)][::-1]
    full = behind + route
    scc = make(full)
    at(scc, full, len(behind), 28.0, 28.0)   # ego at the join, corner ahead
    assert scc.corners, "the corner was truncated away by road behind us"

  def test_the_window_spends_its_budget_along_the_road(self):
    """MUTATION: walk outward by straight-line distance from the car instead of
    by arc length. MEASURED on a route with a hairpin in it: a 150 m budget
    yields 160 m of road by arc length and 237 m by straight line — 58% over,
    because a fold-back stops the crow-flies distance growing while the road
    keeps going.

    Over-spending is not free. The resample budget (MAX_VERTICES) is fixed, so
    a window that runs long gets TRUNCATED AT THE FAR END — which means the
    forward horizon quietly becomes a function of how curvy the road is, and
    the corner that falls off is the distant one the early approach depends on.
    """
    route = bend_route(radius=25.0, arc_deg=180.0, lead_in=20.0)
    tail = [(route[-1][0] + x / M_PER_DEG, route[-1][1]) for x in range(1, 400)]
    xy = RG.to_local(route + tail, 0.0, 0.0, 0.0)
    win = RG.window_around_ego(xy, ahead_m=150.0, behind_m=10.0)
    arc = sum(math.hypot(win[i][0] - win[i - 1][0], win[i][1] - win[i - 1][1])
              for i in range(1, len(win)))
    assert arc <= 150.0 + 10.0 + 5.0, f"spent {arc:.0f} m of a 160 m budget"
    assert arc > 120.0, "and it must actually reach out that far"
    # the crow-flies extent is far smaller here, which is what makes the two
    # criteria differ at all — if this fixture ever stops folding back, the
    # test above stops testing anything
    assert max(math.hypot(x, y) for x, y in win) < arc * 0.75


class TestTheGasGate:
  """The gate tells the planner to stop ADDING speed it is about to give back.
  A human lifts off long before they brake for a bend; the car did not, because
  `gasGating` was published and nothing read it."""

  def test_the_lead_time_is_what_makes_it_early(self):
    """MUTATION: set GATE_LEAD_T to 0. The gate then fires only once the cap is
    ALREADY under us, which is the reactive behaviour this replaced — the car
    holds the throttle until the speed has to come off with the brakes.

    A gentle bend is what shows it: into a hard corner the envelope is under us
    from beyond the lookahead either way, so the lead changes nothing there."""
    # measured: with the lead, approach_cap is 25.3 against a 26.5 threshold;
    # without it, 27.6 — the gate straddles the threshold on this fixture and
    # on nothing sharper, because a hard corner is under us from beyond the
    # lookahead either way
    route = bend_route(radius=250.0, arc_deg=60.0, lead_in=180.0)
    scc = make(route)
    at(scc, route, 0, 27.0, 27.0)
    assert scc.gas_gating_active, "no gate at all — pick a different fixture"
    saved, M.GATE_LEAD_T = M.GATE_LEAD_T, 0.0
    try:
      scc2 = make(route)
      at(scc2, route, 0, 27.0, 27.0)
      assert not scc2.gas_gating_active, "the lead time changed nothing"
    finally:
      M.GATE_LEAD_T = saved

  def test_it_fires_far_out_on_an_approach(self):
    """"Plenty of time ahead": at 60 mph into a bend the gate is on while the
    corner is still hundreds of metres away, so the approach is a coast rather
    than a late brake."""
    route = bend_route(radius=100.0, lead_in=300.0)
    scc = make(route)
    at(scc, route, 0, 27.0, 27.0)
    assert scc.corners and min(c.distance for c in scc.corners) > 250.0
    assert scc.gas_gating_active

  def test_it_does_not_fire_on_a_straight_road(self):
    straight = [(x / M_PER_DEG, 0.0) for x in range(-20, 400)]
    scc = make(straight)
    at(scc, straight, 30, 27.0, 27.0)
    assert not scc.gas_gating_active

  def test_it_does_not_fire_when_we_are_already_slow_enough(self):
    """THE v3.4.8 FAILURE, and the reason this test exists: a gate defined on a
    command rather than on the state it protects fires when there is no
    throttle to cut, pins the accel ceiling to the coast accel, and leaves the
    car unable to accelerate with no way out."""
    route = bend_route(radius=100.0, lead_in=200.0)
    scc = make(route)
    at(scc, route, 0, 8.0, 27.0)      # crawling toward a bend that permits 13
    assert not scc.gas_gating_active

  def test_a_corner_faster_than_the_set_speed_never_gates(self):
    """MUTATION: drop the `v_target >= v_cruise - 0.5` skip.

    THE SKIP ONLY BITES UNDER PEDAL OVERRIDE, and finding that is the point of
    having mutation-tested it: `approach_cap` is never below the corner speed,
    so a corner at or above cruise cannot gate while v_ego <= v_cruise however
    the condition is written. It differs only when the driver is over the set
    speed on the pedal and such a corner is close — and there the gate must
    still be off, because the cap is ignoring that corner and the gate and the
    cap have to agree about which corners exist.
    """
    route = bend_route(radius=100.0, lead_in=60.0)   # permits ~12.9 m/s
    scc = make(route)
    at(scc, route, 0, 16.0, 12.0)                    # pedal override above cruise
    assert scc.corners, "the fixture must actually contain a corner"
    gov = min(scc.corners, key=lambda c: c.v_target)
    assert gov.v_target > 12.0 - 0.5, "the fixture must be a NON-constraining corner"
    assert not scc.gas_gating_active

    # ...and while we are inside it, which is where approach_cap collapses to
    # the corner speed and the two formulations differ most
    for c in scc.corners:
      c.distance = 0.0
    scc._update_gas_gate(16.0, 12.0)
    assert not scc.gas_gating_active, "gated for a corner the cap is ignoring"

  def test_it_has_hysteresis(self):
    """MUTATION: use one threshold for engage and release. The gate then
    limit-cycles at its own boundary — gate off, throttle, cross, gate on,
    coast, drop back — measured at about 1.5 s per cycle, which is squarely in
    the band a passenger feels as surging."""
    assert M.GATE_V_RELEASE < M.GATE_V_MARGIN

  def test_it_cannot_latch(self):
    """A WATCHDOG, NOT A KNOB. A latched throttle gate is a car that will not
    accelerate, which this fork has already shipped once."""
    route = bend_route(radius=60.0, lead_in=300.0)
    scc = make(route)
    held = 0
    for _ in range(int((M.GATE_MAX_S + 5.0) / 0.05)):
      at(scc, route, 0, 27.0, 27.0)    # frozen mid-approach, gate wants to hold
      held += int(scc.gas_gating_active)
    assert not scc.gas_gating_active, "the gate latched"
    assert held * 0.05 <= M.GATE_MAX_S + 0.1

  def test_it_clears_when_the_feature_is_off(self):
    class Off:
      def get_bool(self, key):
        return False
    route = bend_route(radius=100.0, lead_in=200.0)
    scc = M.SCCMapV2(params=Off(), route_reader=lambda: route)
    at(scc, route, 0, 27.0, 27.0)
    assert not scc.gas_gating_active


class TestTheStraightPartOfAnSBend:
  """THE EXPLICIT REQUIREMENT: do not gas it between the two halves of an S.

  Both bends are in the corner list at once, the cap is the min over them, and
  the gate is on whenever either wants us slower — so the car holds the corner
  speed through the middle instead of surging and re-braking.
  """

  def test_both_halves_are_seen_at_once(self):
    route = s_bend_route(between_m=60.0)
    scc = make(route)
    at(scc, route, 120, 27.0, 27.0)
    ahead = [c for c in scc.corners if c.distance > 0]
    assert len(ahead) >= 2, "only one half of the S was in range"
    assert {c.sign for c in scc.corners} != {1}, "the two halves must differ in sign"

  def test_the_cap_does_not_rise_through_the_middle(self):
    """MUTATION: drop corners behind us at the apex, or look only at the
    nearest one. Either way the cap releases in the middle straight and the car
    accelerates into the second half."""
    route = s_bend_route(between_m=60.0)
    scc = make(route)
    for _ in range(4):
      at(scc, route, 300, 12.5, 27.0)   # let CurveSpeedCap latch first
    caps = []
    for i in range(300, 460, 10):     # entry of bend 1 through to bend 2
      at(scc, route, i, 12.5, 27.0)
      caps.append(scc.output_v_target if scc.is_active else 27.0)
    assert max(caps) - min(caps) < 3.0, f"the cap moved by {max(caps) - min(caps):.1f} m/s"

  def test_the_gate_stays_on_through_the_middle(self):
    route = s_bend_route(between_m=60.0)
    scc = make(route)
    # travelling faster than the second half allows, in the straight between
    gated = 0
    for i in range(360, 440, 10):
      at(scc, route, i, 18.0, 27.0)
      gated += int(scc.gas_gating_active)
    assert gated >= 6, "the car was allowed to add throttle between the halves"


class TestTheGateReachesTheThrottle:
  """THE DEFECT THIS RELEASE FIXES, and it is a WIRING defect, so it is pinned
  on the AST rather than by behaviour.

  `longitudinal_planner.py` imports the acados MPC and cannot be constructed
  off-device, so no runtime test in this repo can see the throttle clip. SCC-M
  has published `gasGating` since v0.9.7 and NOTHING EVER READ IT — the clip
  tested SLA's flag alone — and the whole suite was green for four years with
  the feature disconnected. A test that cannot run is exactly how that happens.
  """

  def _clip_block(self):
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[6] / \
        "selfdrive/controls/lib/longitudinal_planner.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
      if not isinstance(node, ast.If):
        continue
      test = ast.unparse(node.test)
      if "gas_gate_active" in test or "gas_gating_active" in test:
        return test, ast.unparse(node)
    return None, None

  def test_the_throttle_clip_reads_scc_m_v2(self):
    test, _body = self._clip_block()
    assert test is not None, "no gas-gate branch found in the planner at all"
    assert "_scc_map_v2.gas_gating_active" in test, (
      "the throttle clip does not read SCC-M v2's gate; the corner cap will"
      + " still be applied but the car will hold throttle up to it")

  def test_it_is_throttle_only(self):
    """The braking floor must be untouched. A gate that can brake is not a
    gate, and lead-following would be riding on it."""
    _test, body = self._clip_block()
    assert "accel_clip[1]" in body
    assert "accel_clip[0] =" not in body, "the gate must never move the braking floor"
    assert "max(accel_coast" in body, "it must clamp to the coast accel, not below"
