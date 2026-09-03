"""FunnyPilot v3.6.2 — SCC-M v2 end to end: measure, cap, drive, learn, repeat.

The pieces are tested in isolation elsewhere (road_geometry, corner_speed,
corner_effort, scc_learn_store). What can only be tested here is that they are
wired the right way round, and four properties in particular:

  1. THE SET SPEED IS A HARD CEILING. Requirement 7, and the one property that
     must hold locally rather than as a consequence of what the governor does
     with the value.
  2. ONLY PASSES OPENPILOT DROVE ARE LEARNED FROM. v3.6.4 reversed the
     original requirement 4 here: a pass the driver steered measures the
     DRIVER, and filing a tired driver's wide line against the corner makes
     that bend permanently slower for a reason that is not about the road.
     Learning is still not gated on the SmartCruiseControlMap toggle — only on
     lateral being active.
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


def straight_route(length_m=400.0, behind_m=20.0):
  """A straight road due north through the origin, ~1 m spacing. A record at
  (d / M_PER_DEG, 0.0) lies exactly on it, d metres ahead."""
  return [(x / M_PER_DEG, 0.0) for x in range(-int(behind_m), int(length_m))]


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
    # lead_in shortened for v3.6.6: the retuned envelope deliberately holds
    # cruise speed further in, so a corner 340 m out no longer dips a full
    # ACTIVATE_MARGIN under the set speed. That is the change, not a defect —
    # TestTheGasGate below pins the coast still starting out there.
    scc = make(bend_route(radius=100.0, lead_in=180.0))
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
             angle=lambda t: 25.0, torque=0.0, lat_active=True,
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


class TestOnlyOpenpilotsOwnPassesAreLearned:
  """v3.6.4 — THIS REVERSES REQUIREMENT 4 OF THE ORIGINAL BRIEF, DELIBERATELY.

  The brief asked for learning regardless of engagement. The owner's
  counter-example is decisive: drift wide through a bend because you are
  tired, distracted or looking at the wrong thing, and every signal still live
  with lateral off reports it as "this corner is too fast" — so the store
  files a permanently slower corner on evidence about the HUMAN.

  It is not partial. `limited` was already gated on lat_active, so with the
  driver steering severity consists ENTIRELY of oscillation and lane
  departure: the two signals that measure the person rather than the road.
  """

  def test_a_hand_driven_pass_is_not_recorded(self, store):
    """MUTATION: drop MIN_ENGAGED_FRAC from CornerPass.usable()."""
    scc = inside_a_corner(store)
    traverse(scc, lat_active=False)
    leave(scc)
    assert store.count == 0

  def test_a_hand_driven_pass_cannot_slow_a_corner(self, store):
    """The direction that matters. A driver sawing his way round a bend must
    not lower that corner's ceiling — that is the change that sticks, and a
    corner that is too slow produces no symptom anyone would notice."""
    scc = inside_a_corner(store)
    traverse(scc, v=22.0, curvature=0.012, lat_active=False,
             angle=lambda t: 25.0 + 5.0 * math.sin(2 * math.pi * 3.0 * t))
    leave(scc)
    assert store.count == 0

  def test_an_engaged_pass_is_still_recorded(self, store):
    """...and the feature still works. MUTATION: gate on the wrong sense of
    lat_active and nothing is ever learned at all."""
    scc = inside_a_corner(store)
    traverse(scc, lat_active=True)
    leave(scc)
    assert store.count == 1

  def test_an_engaged_clean_brisk_pass_raises_the_budget(self, store):
    scc = inside_a_corner(store)
    # 18 m/s at curvature 0.012 -> 3.9 m/s^2, smooth steering
    traverse(scc, v=18.0, curvature=0.012, lat_active=True)
    leave(scc)
    c = next(iter(store.corners.values()))
    assert c.a_lo > CS.A_LAT_DEFAULT

  def test_driving_far_too_fast_while_engaged_teaches_the_ceiling(self, store):
    """The severity arithmetic still answers "how far past"; it just only
    listens to passes openpilot itself drove."""
    scc = inside_a_corner(store)
    traverse(scc, v=22.0, curvature=0.012, lat_active=True,
             angle=lambda t: 25.0 + 5.0 * math.sin(2 * math.pi * 3.0 * t))
    leave(scc)
    c = next(iter(store.corners.values()))
    a_done = 22.0 ** 2 * 0.012
    assert c.a_hi < a_done

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
  @staticmethod
  def _speed_with(store):
    """The corner speed SCC-M v2 prices this bend at, given what `store`
    knows. The SAME road every time, so the lookup lands on the record being
    written; a different lead-in would put the corner somewhere else and the
    comparison would silently be between two unvisited corners."""
    scc = make(bend_route(radius=100.0, lead_in=30.0), store=store)
    run(scc, v_ego=28.0, v_cruise=28.0, n=2)
    assert scc.corners
    return min(c.v_target for c in scc.corners)

  def test_one_clean_pass_does_not_buy_any_speed(self, store):
    """v3.6.2 — THIS IS THE BEHAVIOUR CHANGE, AND IT IS THE POINT.

    Until v3.6.2 a single clean pass immediately bought 45% of the learned
    budget, because `confidence` was visit count alone. One pass has nothing
    to disagree with yet, so it is evidence that the corner is REAL, not
    evidence that we have worked out its speed. It may now floor the fusion's
    corroboration (which is what visits are for) while contributing nothing
    to the speed.

    MUTATION: drop `drift=c.d` from _lookup's effective_a_lat call and this
    fails — the corner gets faster off one pass again."""
    baseline = self._speed_with(None)
    scc = inside_a_corner(store)
    traverse(scc, v=18.0, curvature=0.012)
    leave(scc)
    assert store.count == 1
    assert self._speed_with(store) == pytest.approx(baseline)
    # ...but the corner is still known to be REAL, which is a different claim
    # and the one the fusion consumes.
    assert next(iter(store.corners.values())).n == 1

  def test_repeated_agreeing_passes_do_buy_speed(self, store):
    """The other half: a corner whose passes agree converges, and once it has
    converged the learned budget reaches the cap. Without this the test above
    would be satisfied by learning that never works at all."""
    baseline = self._speed_with(None)
    for _ in range(8):
      scc = inside_a_corner(store)
      traverse(scc, v=18.0, curvature=0.012)
      leave(scc)
    assert store.count == 1
    rec = next(iter(store.corners.values()))
    assert rec.n == 8
    assert rec.d < CS.DRIFT_SETTLED        # it stopped moving
    assert self._speed_with(store) > baseline

  def test_a_third_pass_that_still_moves_the_answer_is_not_confident(self, store):
    """THE REPORTED CASE, PINNED. Three visits used to saturate
    `confidence_for` at 1.0 regardless of whether the answer had settled. Here
    each pass genuinely lowers the ceiling, so the third one is still shifting
    the corner speed — and must not read as certain."""
    scc = inside_a_corner(store)
    traverse(scc, v=18.0, curvature=0.012)
    leave(scc)
    rec = next(iter(store.corners.values()))
    pos = (rec.lat, rec.lon, rec.bearing)
    before = min(rec.a_lo, rec.a_hi)
    # two more passes, each proving the corner supports LESS than we thought
    for i, peak in enumerate((2.6, 2.2)):
      store.observe(*pos, radius=100.0, a_peak=peak, severity=1.6, now=1000.0 + i)
    assert store.count == 1, "the fixture must keep hitting the same record"
    rec = next(iter(store.corners.values()))
    assert rec.n == 3
    # the answer really did move, i.e. this test is not vacuous
    assert abs(min(rec.a_lo, rec.a_hi) - before) > 0.1
    assert CS.confidence_for(rec.n) == 1.0        # the OLD measure: fully certain
    assert CS.confidence_of(rec.n, rec.d) < 0.5   # the new one: still learning

  def test_confidence_cannot_be_withheld_forever(self, store):
    """THE LIVENESS SIDE, and a real property of the interval rather than of
    the drift constants.

    `update_interval` only ever RAISES `a_lo` and only ever LOWERS `a_hi`, both
    clamped to [A_LAT_MIN, A_LAT_MAX]. So `min(a_lo, a_hi)` is monotone and its
    total possible movement is bounded — a corner CANNOT argue with itself
    indefinitely, and drift must therefore decay however adversarial the
    passes are. That is why there is no 'permanently unsettled' failure mode
    where the feature silently refuses to ever learn anything.

    Anyone retuning DRIFT_ALPHA should read that: this bound is what makes the
    EMA safe to make slower."""
    scc = inside_a_corner(store)
    traverse(scc, v=18.0, curvature=0.012)
    leave(scc)
    rec = next(iter(store.corners.values()))
    pos = (rec.lat, rec.lon, rec.bearing)
    # adversarial: shove hard in both directions, alternating, many times
    for i in range(30):
      store.observe(*pos, radius=100.0,
                    a_peak=2.9 if i % 2 else 1.2,
                    severity=0.1 if i % 2 else 2.4, now=1000.0 + i)
    rec = next(iter(store.corners.values()))
    assert rec.d < CS.DRIFT_SETTLED
    assert CS.confidence_of(rec.n, rec.d) == pytest.approx(1.0)

  def test_confidence_is_published_for_the_fusion(self):
    """An unvisited corner must report zero confidence, or scc_fusion would
    let it bypass the vision veto and the junction-jog protection would be
    gone. MUTATION: default gov_confidence to anything above zero."""
    scc = make(bend_route(radius=100.0, lead_in=180.0))
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
    # The fixture has to STRADDLE the threshold, which means re-measuring it
    # whenever the envelope or the default budget move — v3.6.5 changed both,
    # and the old R=250 case stopped gating at all (the assertion below says so
    # out loud rather than reporting a passing test of nothing).
    route = bend_route(radius=130.0, arc_deg=60.0, lead_in=200.0)
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
    corner is still hundreds of metres away, so the approach begins as a coast
    rather than as a late brake.

    v3.6.6 — THE LIFT-OFF STILL COMES FIRST, AND THAT IS THE POINT OF THE
    RETUNE RATHER THAN AN EXCEPTION TO IT. The owner asked to trade a long gas
    -gated coast for a shorter, firmer decel; what that means in the shape is
    that the GATE still opens out here (~360 m) while the CAP now waits until
    ~256 m — the coast phase got shorter at the far end, not deleted."""
    route = bend_route(radius=100.0, lead_in=250.0)
    scc = make(route)
    at(scc, route, 0, 27.0, 27.0)
    assert scc.corners and min(c.distance for c in scc.corners) > 250.0
    assert scc.gas_gating_active
    # ...and the cap is NOT yet constraining at that distance
    assert not scc.is_active

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


class TestTheDistancesDoNotGoStale:
  """FunnyPilot v3.6.5 — the geometry is throttled, the cap is not.

  `_refresh_corners` runs at most every _GEOM_PERIOD_S because its source is
  1 Hz data, but `update()` runs every model frame and nothing used to move
  `distance` in between. The envelope was therefore fed a distance that was
  correct at the refresh and up to `v_ego * _GEOM_PERIOD_S` metres too LARGE by
  the end of the interval — 13.4 m at 26.8 m/s, and always in the loose
  direction, because the number only ever ages toward "further away than it is".

  Worth 0.9 m/s of extra speed on the steep part of the envelope, delivered as
  a 0.5 s staircase. Half of "it is often too late to decelerate".
  """

  def _placed(self, d=200.0, v_ego=25.0):
    scc = make(bend_route(radius=90.0))
    run(scc, v_ego=v_ego, v_cruise=30.0, n=1)
    assert scc.corners
    for c in scc.corners:
      c.distance = d
    return scc

  def test_update_closes_the_distance_on_a_non_refresh_frame(self):
    """THE WIRING, NOT THE FUNCTION — and the distinction is not academic. The
    first version of this class called `_dead_reckon` directly and SURVIVED the
    mutation that deletes the call from `update()`: a test that reaches past
    the call site cannot see the call site. So this one drives the real entry
    point with the geometry throttle engaged, exactly as it is between the two
    refreshes of any real second of driving.
    """
    import time as _t
    scc = self._placed(d=200.0, v_ego=25.0)
    now = _t.monotonic()
    scc._geom_at = now          # a refresh just happened; this frame skips it
    scc._dr_at = now - 0.1
    scc.update(True, 25.0, 0.0, 30.0, 0.0, 0.0, 0.0, True)
    assert scc.corners[0].distance == pytest.approx(200.0 - 2.5, abs=0.3)

  def test_the_function_subtracts_exactly_what_was_driven(self):
    """MUTATION: change the sign, or scale it. 25 m/s for 0.1 s is 2.5 m."""
    scc = self._placed(d=200.0, v_ego=25.0)
    scc._dr_at = 100.0
    scc._dead_reckon(100.1, 25.0)
    assert scc.corners[0].distance == pytest.approx(200.0 - 2.5)

  def test_it_is_exactly_the_distance_travelled(self):
    """Not a fudge factor: 20 m/s for 0.25 s is 5 m, and nothing else."""
    scc = self._placed(d=300.0)
    scc._dr_at = 50.0
    scc._dead_reckon(50.25, 20.0)
    assert scc.corners[0].distance == pytest.approx(295.0)

  def test_a_refresh_frame_advances_nothing(self):
    """_refresh_corners re-stamps `_dr_at`, so the frame that recomputed the
    distances from the live GPS must not then subtract from them as well.
    MUTATION: drop the `self._dr_at = now` line in _refresh_corners and every
    refresh double-counts up to half a second of travel."""
    scc = make(bend_route(radius=90.0))
    run(scc, v_ego=25.0, v_cruise=30.0, n=1)
    before = [c.distance for c in scc.corners]
    scc._geom_at = 0.0
    scc.update(True, 25.0, 0.0, 30.0, 0.0, 0.0, 0.0, True)
    after = [c.distance for c in scc.corners]
    assert after == pytest.approx(before)

  def test_a_clock_jump_is_rejected_not_applied(self):
    """Over-closing the distance would tighten the cap on evidence we do not
    have, so an implausible dt does nothing at all."""
    scc = self._placed(d=200.0)
    scc._dr_at = 10.0
    scc._dead_reckon(400.0, 25.0)          # 390 s of 'travel'
    assert scc.corners[0].distance == pytest.approx(200.0)

  def test_standing_still_moves_nothing(self):
    scc = self._placed(d=120.0)
    scc._dr_at = 10.0
    scc._dead_reckon(10.2, 0.0)
    assert scc.corners[0].distance == pytest.approx(120.0)

  def test_it_tightens_the_cap_rather_than_loosening_it(self):
    """The direction is the whole point. A stale distance can only ever say the
    corner is further away than it is, i.e. permit more speed."""
    scc = self._placed(d=150.0, v_ego=25.0)
    stale = scc._raw_cap(30.0)
    scc._dr_at = 100.0
    scc._dead_reckon(100.4, 25.0)
    assert scc._raw_cap(30.0) < stale


class TestTheExitHandsAuthorityBack:
  """v3.6.5 — the controller-side half of TestTheRunOut in test_corner_speed."""

  def _past(self, past_m, half=20.0, v_target=13.0):
    scc = make(bend_route(radius=90.0))
    run(scc, v_ego=25.0, v_cruise=30.0, n=1)
    assert scc.corners
    scc.corners = scc.corners[:1]
    c = scc.corners[0]
    c.half_len, c.v_target, c.distance = half, v_target, -(half + past_m)
    return scc

  def test_the_cap_climbs_as_the_corner_recedes(self):
    """MUTATION: restore `approach_cap(v, max(distance, 0))` in _raw_cap. Every
    value below becomes exactly v_target and the ordering assertion fails.
    v3.7.1: at the exit point the cap is already the EXIT speed — the ramp from
    the apex has run — and the release continues from there."""
    caps = [self._past(s)._raw_cap(30.0) for s in (0.0, 10.0, 25.0, 60.0)]
    assert caps[0] == pytest.approx(CS.exit_speed(13.0, 20.0))
    assert caps[0] > 13.0
    assert all(b > a for a, b in zip(caps, caps[1:], strict=False))

  def test_the_gate_lets_go_on_the_way_out_too(self):
    """The gas gate is a THROTTLE clip, so a gate still holding on the exit
    would undo the cap's release. MUTATION: leave `max(c.distance, 0.0)` in
    _update_gas_gate and the car coasts out of every bend."""
    scc = self._past(40.0)
    scc.is_enabled = True
    scc._update_gas_gate(v_ego=15.0, v_cruise=30.0)
    assert not scc.gas_gating_active

  def test_the_gate_still_holds_on_the_way_in(self):
    """...and the anti-mutation half: it must not have simply stopped working."""
    scc = self._past(0.0)
    scc.corners[0].distance = 120.0
    scc.is_enabled = True
    scc._update_gas_gate(v_ego=27.0, v_cruise=30.0)
    assert scc.gas_gating_active


def route_with_tail(radius=100.0, arc_deg=90.0, lead_in=150.0, tail_m=300.0):
  """bend_route plus a straight run-out heading east after the bend, so a car
  can be placed PAST the corner and still be on the route."""
  pts = bend_route(radius=radius, arc_deg=arc_deg, lead_in=lead_in)
  end_n, end_e = lead_in + radius, radius       # where a 90-degree right-hander ends
  for t in range(1, int(tail_m)):
    pts.append((end_n / M_PER_DEG, (end_e + t) / M_PER_DEG))
  return pts


class TestACornerIsListedForAsLongAsItCaps:
  """v3.7.1 — THE MAP PILL WITH NO CORNER ON THE MINIMAP. The corner left the
  list BEHIND_KEEP_M past its exit while the cap went on releasing for several
  seconds; the pill (and the LRN pill) read the cap, the minimap reads the
  list. A corner now stays listed while its run-out is under the set speed, up
  to BEHIND_MAX_M, so the two cannot disagree.
  """

  def _past_the_exit(self, tail_m, v_cruise):
    """Stand `tail_m` down the straight tail after the bend, heading east. The
    apex is then roughly (arc/2 + tail_m) behind: 78 + tail_m for this route."""
    route = route_with_tail()
    scc = make(route)
    run(scc, v_ego=20.0, v_cruise=v_cruise, n=1, lat=0.0, lon=0.0, bearing=0.0)
    assert scc.corners, "fixture: the bend must be found"
    c = scc.corners[0]
    run(scc, v_ego=20.0, v_cruise=v_cruise, n=1,
        lat=250.0 / M_PER_DEG, lon=(100.0 + tail_m) / M_PER_DEG, bearing=90.0)
    return scc, c

  def test_the_pure_rule(self):
    """MUTATION: return `d >= -(half + BEHIND_KEEP_M)` only."""
    keep = M.SCCMapV2._keep_behind
    v, half = 13.0, 20.0
    assert keep(50.0, half, v, 30.0)                       # ahead: always
    assert keep(-(half + M.BEHIND_KEEP_M), half, v, 30.0)  # inside the keep: always
    # past the keep but the run-out is still under cruise: kept
    d = -(half + M.BEHIND_KEEP_M + 40.0)
    assert CS.corner_cap(v, d, half) < 30.0
    assert keep(d, half, v, 30.0)
    # ...and NOT kept once the run-out has reached the set speed
    assert CS.corner_cap(v, d, half) > 20.0
    assert not keep(d, half, v, 20.0)
    # ...nor ever beyond the hard bound, however high the set speed
    assert not keep(-(half + M.BEHIND_MAX_M + 1.0), half, v, 1000.0)

  def test_a_corner_still_capping_stays_on_the_list(self):
    """MUTATION: drop the `_keep_behind` call from _refresh_corners (i.e. cut
    at BEHIND_KEEP_M again)."""
    # 90 m down the tail: the detected extent of this 90-degree arc is ~108 m
    # each side of the apex, so this is the first probe clear of BEHIND_KEEP_M.
    scc, c = self._past_the_exit(90.0, v_cruise=30.0)
    behind = [k for k in scc.corners if k.distance < -(k.half_len + M.BEHIND_KEEP_M)]
    assert behind, [(k.distance, k.half_len) for k in scc.corners]
    k = behind[0]
    assert CS.corner_cap(k.v_target, k.distance, k.half_len) < 30.0
    # and the cap is real: SCC-M is still active on it
    assert scc.is_active

  def test_the_geometry_window_reaches_back_far_enough_to_keep_it(self):
    """THE HALF OF THE SYMPTOM THE KEEP RULE ALONE COULD NOT FIX. The corner
    list is rebuilt from the route polyline inside `window_around_ego`, and at
    120 m of behind-window a bend whose apex had passed that far back simply
    stopped being FOUND — list empty, cap still releasing, pill still lit.
    The window must reach at least as far back as anything `_keep_behind` can
    keep. MUTATION: WINDOW_BEHIND_M back to 120."""
    assert RG.WINDOW_BEHIND_M >= M.BEHIND_MAX_M + 2 * 60.0, (RG.WINDOW_BEHIND_M, M.BEHIND_MAX_M)
    scc, c = self._past_the_exit(140.0, v_cruise=30.0)   # apex ~220 m behind
    behind = [k for k in scc.corners if k.distance < -(k.half_len + M.BEHIND_KEEP_M)]
    assert behind, [(k.distance, k.half_len) for k in scc.corners]
    assert scc.is_active

  def test_a_corner_whose_run_out_is_done_is_dropped(self):
    """The same geometry at a set speed the run-out has already reached."""
    scc, c = self._past_the_exit(90.0, v_cruise=12.0)
    assert not any(k.distance < -(k.half_len + M.BEHIND_KEEP_M) for k in scc.corners)

  def test_the_gas_gate_and_the_warning_ignore_a_corner_behind(self):
    """Keeping it listed must not let it gate the throttle or raise the
    unmanageable warning — both are approach-side questions."""
    scc, c = self._past_the_exit(90.0, v_cruise=30.0)
    scc.is_enabled = True
    scc._update_gas_gate(v_ego=20.0, v_cruise=30.0)
    assert not scc.gas_gating_active
    for k in scc.corners:
      k.unmanageable = True
    scc._update_warning(20.0)
    assert not scc.corner_warning


def orphan_drive(scc, seconds=4.0, dt=0.01, v=20.0, curvature=0.012,
                 lat_active=True, torque=0.0, saturated=False, t0=500.0,
                 lat=45.0, lon=-93.0):
  """Drive a bend the corner list knows nothing about."""
  n = int(seconds / dt)
  for i in range(n):
    scc.observe_frame(t0 + i * dt, v, curvature, 25.0, torque, lat_active,
                      saturated, False, False, False, 3.0,
                      lat=lat, lon=lon, bearing=90.0)


class TestBendsTheGeometryNeverSaw:
  """FunnyPilot v3.6.5 — the structural gap this closes.

  A pass only ever opened INSIDE a listed corner, and the radius estimator has
  a documented blind spot on short-sweep bends (v3.6.4: a true R=40 reading as
  R=172). So a corner it misses was missed FOREVER: nothing capped for it, and
  because no pass opened there, nothing learned it existed either. The owner's
  report is that exact loop — the car runs wide, the driver grabs it, and the
  next visit is identical.
  """

  def test_a_stressed_unlisted_bend_is_recorded(self, store):
    """MUTATION: delete the _observe_orphan call from observe_frame."""
    scc = make([], store=store)
    orphan_drive(scc, saturated=True)
    orphan_drive(scc, seconds=1.0, curvature=0.0, t0=504.0)
    assert store.count == 1
    assert scc.orphan_count == 1

  def test_the_radius_comes_from_the_car_not_the_map(self):
    """`controlsState.curvature` is the vehicle model's reading of the steering
    angle, so R = 1/|k| at the tightest point is what the car ACTUALLY drove —
    no node spacing, no smoothing window, no aliasing. That is why an orphan
    record is worth having at all."""
    with tempfile.TemporaryDirectory() as d:
      s = LS.LearnStore(directory=d, name="corners_v2.jsonl")
      scc = make([], store=s)
      orphan_drive(scc, curvature=0.01, saturated=True)
      orphan_drive(scc, seconds=1.0, curvature=0.0, t0=504.0)
      rec = next(iter(s.corners.values()))
      assert rec.r == pytest.approx(100.0, abs=1.0)
      s.join_writes()

  def test_a_clean_unlisted_bend_is_not_recorded(self, store):
    """THE ONE EXTRA GATE AN ORPHAN HAS, and the owner's rule: a listed corner
    records every usable pass because its existence is established, while an
    orphan's existence is being ASSERTED by the record — so it has to have
    actually hurt. MUTATION: drop the `severity < 1.0` return."""
    scc = make([], store=store)
    orphan_drive(scc)
    orphan_drive(scc, seconds=1.0, curvature=0.0, t0=504.0)
    assert store.count == 0

  def test_a_straight_road_is_never_an_orphan(self, store):
    """ORPHAN_K_MIN. MUTATION: set it to 0 and every metre of road becomes a
    candidate corner."""
    scc = make([], store=store)
    # BETWEEN ORPHAN_K_END and ORPHAN_K_MIN ON PURPOSE. The first version of
    # this used 0.0005, which is under BOTH — so the orphan opened and closed on
    # the same frame either way and the mutation survived. Sustained curvature
    # in the band is the only fixture that can see the threshold.
    assert M.ORPHAN_K_END < 0.0025 < M.ORPHAN_K_MIN
    orphan_drive(scc, seconds=6.0, curvature=0.0025, saturated=True)
    assert scc._orphan is None
    assert store.count == 0

  def test_a_listed_corner_is_not_double_counted(self, store):
    """The orphan observer only runs when no pass is open, so a bend the
    geometry DID find is measured once, by the normal path."""
    scc = inside_a_corner(store)
    traverse(scc, v=18.0, curvature=0.012, lat_active=True, saturated=True)
    leave(scc)
    assert scc.orphan_count == 0
    assert store.count == 1


class TestLearnedCornersEnterTheList:
  """v3.6.5 — the store is a SOURCE of corners now, not only an annotation.

  Without this an orphan record is data nothing reads: `_refresh_corners` built
  the list from the geometry alone, so a bend the geometry misses could never
  cap the car however many times it had been measured — and could never show a
  ring on the minimap either, since the ring needs `visits >= 1` on a LISTED
  corner.

  v3.7.1 — AND A RECORD HAS TO BE ON THE ROAD WE ARE ON. The first cut took any
  record within 400 m whose heading roughly matched ours; that is also a bend
  on a parallel road, and the car capped for corners the minimap could not
  show. Records are matched to mapd's route polyline now (STORE_ROUTE_MAX_M),
  their distance is arc length along it, and their heading is compared with
  the route's direction where they sit.
  """

  def _seeded(self, d, radius=90.0, lon_m=0.0, bearing=0.0):
    tmp = tempfile.TemporaryDirectory()
    s = LS.LearnStore(directory=tmp.name, name="corners_v2.jsonl")
    # a record ahead (bearing 0 => north => +lat), `lon_m` metres to the side
    s.observe(d / M_PER_DEG, lon_m / M_PER_DEG, bearing, radius, 2.0, 0.2)
    return s, tmp

  def test_a_learned_bend_the_geometry_missed_still_caps(self):
    """MUTATION: delete the `_corners_from_store` call from
    _refresh_corners. The route is a dead straight: the geometry finds
    nothing, the store record is what puts a corner in the list."""
    s, tmp = self._seeded(150.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=28.0, v_cruise=28.0, n=5)
      assert scc.corners, "the store must be able to put a corner in the list"
      assert scc.corners[0].visits >= 1
      assert scc.is_active
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_it_carries_the_visits_the_ring_is_drawn_from(self):
    """THE MINIMAP SIDE OF THE SAME FIX. `learned_corners_from` filters on
    field 6 (visits) of fp_corners, and only a LISTED corner is ever written
    there."""
    s, tmp = self._seeded(200.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=28.0, v_cruise=28.0, n=3)
      assert any(c.visits >= 1 for c in scc.corners)
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_geometry_wins_where_both_describe_the_same_bend(self):
    """STORE_DEDUPE_M. The polyline apex is a live projection from the current
    pose; a record's position is where the car was on an earlier drive. Keeping
    both would cap one bend twice. MUTATION: drop the dedupe."""
    route = bend_route(radius=100.0, lead_in=150.0)
    scc = make(route)
    run(scc, v_ego=28.0, v_cruise=28.0, n=2)
    geom_n = len(scc.corners)
    apex = min(scc.corners, key=lambda c: abs(c.distance))
    with tempfile.TemporaryDirectory() as d:
      s = LS.LearnStore(directory=d, name="corners_v2.jsonl")
      s.observe(apex.lat, apex.lon, apex.bearing, 100.0, 2.0, 0.2)
      scc2 = make(route, store=s)
      run(scc2, v_ego=28.0, v_cruise=28.0, n=2)
      assert len(scc2.corners) == geom_n
      s.join_writes()

  def test_a_record_behind_us_is_not_injected(self):
    s, tmp = self._seeded(-150.0)
    try:
      scc = make(straight_route(behind_m=300.0), store=s)
      run(scc, v_ego=28.0, v_cruise=28.0, n=3)
      assert not scc.corners
    finally:
      s.join_writes()
      tmp.cleanup()

  # ── v3.7.1: on THIS road ──────────────────────────────────────────────────

  def test_a_record_beside_the_road_is_not_injected(self):
    """THE REPORTED CASE. A bend on a parallel road 100 m to the side, same
    heading, 150 m ahead: the old projection put it 150 m ahead ON our road.
    MUTATION: drop the STORE_ROUTE_MAX_M test."""
    s, tmp = self._seeded(150.0, lon_m=100.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=28.0, v_cruise=28.0, n=5)
      assert not scc.corners
      assert not scc.is_active
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_a_record_a_lane_off_the_centreline_still_counts(self):
    """GPS accuracy is up to MAX_GPS_ACC_M and OSM ways are centrelines: a real
    record on our road sits a few metres off the polyline. The bound must not
    reject it. MUTATION: set STORE_ROUTE_MAX_M under ~12."""
    s, tmp = self._seeded(150.0, lon_m=8.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=28.0, v_cruise=28.0, n=5)
      assert scc.corners
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_no_route_means_no_injection(self):
    """Without a route there is nothing to say which road a record is on.
    MUTATION: fall back to the along-heading projection when `rs` is empty."""
    s, tmp = self._seeded(150.0)
    try:
      scc = make([], store=s)
      run(scc, v_ego=28.0, v_cruise=28.0, n=5)
      assert not scc.corners
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_the_distance_is_arc_length_along_the_route(self):
    """A record on the far side of a bend is further away BY ROAD than by air.
    The old projection understated it; the envelope is built on distance, so
    that was a cap arriving early. MUTATION: use the straight-line projection."""
    route = bend_route(radius=100.0, arc_deg=90.0, lead_in=150.0)
    # the geometry finds the bend itself; put the record well past it, on the
    # route's tail, and out of STORE_DEDUPE_M of the apex
    tail = route[-1]
    with tempfile.TemporaryDirectory() as d:
      s = LS.LearnStore(directory=d, name="corners_v2.jsonl")
      s.observe(tail[0], tail[1], 90.0, 60.0, 2.0, 0.2)     # heading east at the exit
      scc = make(route, store=s)
      run(scc, v_ego=28.0, v_cruise=28.0, n=2)
      rec = [c for c in scc.corners if c.visits >= 1]
      assert rec, "the record on the route's tail must be listed"
      straight = math.hypot(tail[0] * M_PER_DEG, tail[1] * M_PER_DEG)
      assert rec[0].distance > straight + 15.0, (rec[0].distance, straight)
      s.join_writes()

  def test_a_records_heading_is_judged_where_it_sits_not_where_we_are(self):
    """The other carriageway, or the same bend southbound, is a different
    approach and must not be injected — but the test has to be the ROUTE'S
    direction at the record, not ours here. Past a 90-degree right-hander the
    route runs east while we still head north: a record heading east there is
    ours and must be listed, one heading west is the opposite carriageway.
    MUTATION: compare the record's heading with OUR bearing — the eastbound
    record is then 90 degrees off and wrongly rejected."""
    route = route_with_tail(radius=100.0, arc_deg=90.0, lead_in=150.0, tail_m=120.0)
    # the tail starts at (250, 100) heading east; put the record 80 m along it
    rec_lat, rec_lon = 250.0 / M_PER_DEG, 180.0 / M_PER_DEG
    for heading, expect in ((90.0, True), (270.0, False)):
      with tempfile.TemporaryDirectory() as d:
        s = LS.LearnStore(directory=d, name="corners_v2.jsonl")
        s.observe(rec_lat, rec_lon, heading, 60.0, 2.0, 0.2)
        scc = make(route, store=s)
        run(scc, v_ego=28.0, v_cruise=28.0, n=2)
        listed = any(c.visits >= 1 for c in scc.corners)
        assert listed == expect, (heading, [(c.distance, c.visits) for c in scc.corners])
        s.join_writes()


class TestTheUnmanageableCornerWarning:
  """v3.6.5 — a bend whose learned budget has bottomed out and which STILL
  stresses the car cannot be fixed by slowing down: A_LAT_MIN is the floor by
  construction. So the driver is told, on the APPROACH."""

  def _at_floor(self, d, radius=90.0, visits=3):
    tmp = tempfile.TemporaryDirectory()
    s = LS.LearnStore(directory=tmp.name, name="corners_v2.jsonl")
    for _ in range(visits):
      s.observe(d / M_PER_DEG, 0.0, 0.0, radius, 1.0, 3.0)
    return s, tmp

  def test_the_rule_has_one_home(self):
    """Corners reach the list by two routes now — the route geometry and the
    store — and the first cut of this tested the same thing inline in both. A
    mutation to one copy left the suite green through the other."""
    assert M.is_unmanageable(1.0, 1.0, 3)
    assert not M.is_unmanageable(1.0, 1.0, 1)          # one bad pass is not a pattern
    assert not M.is_unmanageable(2.4, 2.4, 9)          # an ordinary learned bend
    assert not M.is_unmanageable(None, 1.0, 3)         # garbage never warns

  def test_a_bottomed_out_bend_ahead_warns(self):
    """MUTATION: drop the `unmanageable` term from _update_warning."""
    s, tmp = self._at_floor(100.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=25.0, v_cruise=25.0, n=3)
      assert scc.corners and scc.corners[0].unmanageable
      assert scc.corner_warning
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_an_ordinary_learned_bend_does_not(self):
    """MUTATION: drop the UNMANAGEABLE_A_LAT test and every learned corner
    raises a banner, which is how a warning becomes noise."""
    tmp = tempfile.TemporaryDirectory()
    try:
      s = LS.LearnStore(directory=tmp.name, name="corners_v2.jsonl")
      for _ in range(3):
        s.observe(100.0 / M_PER_DEG, 0.0, 0.0, 90.0, 2.4, 0.1)
      scc = make(straight_route(), store=s)
      run(scc, v_ego=25.0, v_cruise=25.0, n=3)
      assert not scc.corner_warning
      s.join_writes()
    finally:
      tmp.cleanup()

  def test_one_bad_visit_is_not_a_pattern(self):
    """UNMANAGEABLE_VISITS. A single catastrophic pass SEEDS the ceiling
    outright, so without this the first bad sample warns forever."""
    s, tmp = self._at_floor(100.0, visits=1)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=25.0, v_cruise=25.0, n=3)
      assert not scc.corner_warning
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_it_warns_before_the_entry_not_inside_the_bend(self):
    """WARN_LEAD_T is SECONDS to the corner's ENTRY. Being told about a bend
    once you are in it is the complaint this exists to fix."""
    s, tmp = self._at_floor(350.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=10.0, v_cruise=25.0, n=3)   # 350 m is 35 s away at 10 m/s
      assert not scc.corner_warning
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_the_cap_is_not_removed(self):
    """THE ONE PLACE THIS DELIBERATELY STOPS SHORT OF "stop trying to slow
    down": taking an existing constraint off a bend the car has repeatedly
    failed is the version of that which could hurt someone. The floor cap
    stays; only the escalation and the silence end."""
    s, tmp = self._at_floor(100.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=25.0, v_cruise=25.0, n=6)
      assert scc.corner_warning
      assert scc.is_active and scc.output_v_target < 25.0
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_it_latches_so_the_banner_cannot_flicker(self):
    """The geometry refresh moves a corner's distance across the threshold; a
    warning that blinks reads as a glitch. MUTATION: drop WARN_MIN_S."""
    s, tmp = self._at_floor(100.0)
    try:
      scc = make(straight_route(), store=s)
      run(scc, v_ego=25.0, v_cruise=25.0, n=3)
      assert scc.corner_warning
      # gps_ok=False empties the corner list for real. Clearing `scc.corners` by
      # hand does NOT: run() forces a geometry refresh, which rebuilds the list
      # from the store, so the first version of this test never presented an
      # empty list to _update_warning and the mutation survived.
      run(scc, v_ego=25.0, v_cruise=25.0, n=1, gps_ok=False)
      assert not scc.corners
      assert scc.corner_warning, "it must hold for WARN_MIN_S"
    finally:
      s.join_writes()
      tmp.cleanup()


class TestAManualSpeedOnlyEverRaises:
  """FunnyPilot v3.6.6 — the owner's rule, verbatim: "if we take manual control
  of long going through a corner, the speed we take it at should only ever raise
  the corner's speed. If we have a 45 mph recognised corner and take long
  control over and go 35 through it, the corner's speed should remain at 45. If
  we instead accelerated to 50 and the lateral control was able to keep us on
  track, it should be raised to 50."

  It is implemented as a SPLIT rather than a new signal: a LATERAL handover
  still truncates the pass and condemns the corner (v3.6.5), while a
  LONGITUDINAL one leaves the pass running — lateral is still ours, so the
  effort signals are still meaningful — and makes it raise-only.
  """

  def _learned(self, d, a_lo=2.0, a_hi=2.2):
    tmp = tempfile.TemporaryDirectory()
    s = LS.LearnStore(directory=tmp.name, name="corners_v2.jsonl")
    key = s.observe(d / M_PER_DEG, 0.0, 0.0, 90.0, a_lo, 0.2)
    c = s.corners[key]
    c.a_lo, c.a_hi = a_lo, a_hi
    return s, tmp, key

  def test_going_slower_by_hand_leaves_the_corner_alone(self):
    """MUTATION: drop `allow_lower` from the observe() call, or from the store.
    The braking pass then drags the ceiling down and a 45 mph bend becomes a
    35 mph bend permanently."""
    s, tmp, key = self._learned(100.0)
    try:
      before = (s.corners[key].a_lo, s.corners[key].a_hi)
      # a hard, stressed pass at a LOW peak — the shape a cautious manual
      # traversal has — with allow_lower off
      s.observe(100.0 / M_PER_DEG, 0.0, 0.0, 90.0, 1.2, 2.5, allow_lower=False)
      assert (s.corners[key].a_lo, s.corners[key].a_hi) == pytest.approx(before)
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_the_same_pass_would_have_lowered_it_otherwise(self):
    """Anti-vacuous: the mutation has to be able to change something."""
    s, tmp, key = self._learned(100.0)
    try:
      s.observe(100.0 / M_PER_DEG, 0.0, 0.0, 90.0, 1.2, 2.5, allow_lower=True)
      assert s.corners[key].a_hi < 2.2
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_going_faster_by_hand_does_raise_it(self):
    """The other half. A clean pass at a higher peak still moves the floor,
    and `seed` (the demonstration) adopts it outright."""
    s, tmp, key = self._learned(100.0)
    try:
      s.observe(100.0 / M_PER_DEG, 0.0, 0.0, 90.0, 2.7, 0.1,
                allow_lower=False, seed=True)
      assert s.corners[key].a_lo == pytest.approx(2.7)
    finally:
      s.join_writes()
      tmp.cleanup()

  def test_the_wheel_is_still_a_takeover(self):
    """v3.6.5's rule survives untouched: LATERAL is the axis that condemns."""
    e = M.LateralEffort()
    e.update(0.01, 20.0, 0.005, 0.0, 0.0, False)
    assert e.override
    e2 = M.LateralEffort()
    e2.update(0.01, 20.0, 0.005, 0.0, 0.0, True, long_active=False)
    assert not e2.override and e2.long_manual

  def test_the_flag_reaches_the_store(self):
    """MUTATION: pass `allow_lower=True` unconditionally at the call site.
    Wiring, not arithmetic — the arithmetic is tested above."""
    import ast
    import inspect
    src = inspect.getsource(M.SCCMapV2._commit_pass)
    tree = ast.parse(src.strip())
    found = False
    for node in ast.walk(tree):
      if isinstance(node, ast.keyword) and node.arg == "allow_lower":
        found = not isinstance(node.value, ast.Constant)
    assert found, "_commit_pass must pass a computed allow_lower to observe()"


class TestATurnIsNotAPass:
  """FunnyPilot v3.7.0 — the corner by the owner's house.

  The driveway sits on the bend, so most visits are a TURN into or out of it:
  blinker on, braking, the driver steering off the road. SCC-M had learned the
  corner as far slower than it is. `blinker` used to be folded into `blocked`,
  which rejected the pass at commit — but only for frames while the stalk was
  physically on, and self-cancelling stalks switch off near the END of the
  turn while the manoeuvre carries on for seconds.
  """

  def _frames(self, scc, n, blinker=False, lat_active=True, curvature=0.012, t0=100.0, dt=0.01):
    for i in range(n):
      scc.observe_frame(t0 + i * dt, 18.0, curvature, 25.0, 0.0, lat_active, False,
                        False, blinker, False, 3.0)
    return t0 + n * dt

  def test_a_blinker_abandons_an_open_pass_immediately(self, store):
    scc = inside_a_corner(store)
    self._frames(scc, 200)                         # two seconds into a real pass
    assert scc._pass.open
    scc.observe_frame(102.0, 18.0, 0.012, 25.0, 0.0, True, False, False, True, False, 3.0)
    assert not scc._pass.open and scc._pass_key is None
    leave(scc)
    assert store.count == 0

  def test_nothing_opens_while_the_blinker_is_on(self, store):
    scc = inside_a_corner(store)
    self._frames(scc, 300, blinker=True)
    assert not scc._pass.open

  def test_the_hold_outlasts_a_self_cancelling_stalk(self, store):
    """THE FIX. The stalk cancels near the end of the turn; the manoeuvre
    continues. A pass opening in that tail must not be judged as a drive
    through the bend. MUTATION: TURN_HOLD_S -> 0."""
    # THE PROBE DURATION IS FIXED, NOT DERIVED FROM THE CONSTANT UNDER TEST. A
    # first version ran `(TURN_HOLD_S - 0.5) / dt` frames of "hold", which is
    # -50 frames when the constant is mutated to zero — so it ran nothing and
    # the assertion held vacuously. The mutation survived. A guard whose
    # fixture collapses when the constant is removed is not a guard.
    assert M.TURN_HOLD_S >= 1.5, "the 1.0 s probe below must land well inside the hold"
    scc = inside_a_corner(store)
    t = self._frames(scc, 50, blinker=True)                   # 0.5 s of stalk
    t = self._frames(scc, 100, blinker=False, t0=t)           # 1.0 s after it cancels
    assert not scc._pass.open, "still inside the hold; nothing may open"
    t = self._frames(scc, int(M.TURN_HOLD_S / 0.01) + 50, blinker=False, t0=t)   # past the hold
    assert scc._pass.open, "anti-vacuous: once the hold lapses, learning resumes"

  def test_a_turn_records_nothing_in_either_direction(self, store):
    # Abandon, not raise-only: a turn is silent about the corner.
    scc = inside_a_corner(store)
    self._frames(scc, 200)
    self._frames(scc, 400, blinker=True, t0=102.0, curvature=0.08)   # turning off, hard
    leave(scc)
    assert store.count == 0

  def test_an_orphan_candidate_is_dropped_by_a_blinker(self, store):
    """The orphan learner watches unlisted bends; a junction turn is the
    tightest 'bend' a car ever drives and would file a very slow record."""
    scc = make(bend_route(radius=600.0, arc_deg=5.0, lead_in=800.0), store=store)
    scc.corners = []
    scc._read_route = list
    t = 100.0
    for i in range(60):                                    # a tight turn, engaged
      scc.observe_frame(t + i * 0.01, 9.0, 0.09, 30.0, 0.0, True, False, False, False, False, 3.0,
                        lat=37.5, lon=-122.0, bearing=90.0)
    assert scc._orphan is not None
    scc.observe_frame(t + 0.6, 9.0, 0.09, 30.0, 0.0, True, False, False, True, False, 3.0,
                      lat=37.5, lon=-122.0, bearing=90.0)
    assert scc._orphan is None
    assert store.count == 0

  def test_an_unsignalled_driveway_takeover_is_a_turn_off(self, store):
    """No blinker to abandon on, so the steering has to tell it. Engaged into
    the bend, then the driver grabs the wheel and turns at junction curvature:
    the takeover verdict (severity 2.0) must NOT condemn the corner."""
    scc = inside_a_corner(store)
    self._frames(scc, 150)                                              # ours, engaged
    for i in range(60):                                                 # driver steers off, hard
      scc.observe_frame(101.5 + i * 0.01, 12.0, 0.09, 40.0, 0.0, False, False,
                        False, False, False, 3.0)
    leave(scc)
    assert store.count == 0

  def test_a_genuine_mid_bend_takeover_is_still_a_verdict(self, store):
    """Anti-vacuous for the one above: a takeover at the bend's OWN curvature is
    the driver saying it was too fast, and v3.6.5's verdict must survive."""
    scc = inside_a_corner(store)
    self._frames(scc, 150)
    for i in range(60):
      scc.observe_frame(101.5 + i * 0.01, 18.0, 0.012, 25.0, 0.0, False, False,
                        False, False, False, 3.0)
    leave(scc)
    assert store.count == 1
    c = next(iter(store.corners.values()))
    assert c.a_hi < CS.A_LAT_MAX

  def test_an_orphan_filed_from_a_manual_long_pass_is_raise_only(self, store):
    """The v3.6.6 rule the orphan path was missing. MUTATION: drop
    `allow_lower=not p.long_manual` from _commit_orphan."""
    scc = make(bend_route(radius=600.0, arc_deg=5.0, lead_in=800.0), store=store)
    scc.corners = []
    scc._read_route = list
    # pre-existing record at the spot, with a known ceiling
    store.observe(37.5, -122.0, 90.0, 40.0, 2.0, 0.0, now=1.0)
    hi_before = next(iter(store.corners.values())).a_hi
    t = 100.0
    # an engaged, stressed, BRAKED pass through an unlisted bend right there
    for i in range(400):
      scc.observe_frame(t + i * 0.01, 12.0, 0.03, 25.0 + (8.0 if (i // 5) % 2 else -8.0), 0.0,
                        True, False, False, False, False, 3.0, brake_pressed=True,
                        lat=37.5, lon=-122.0, bearing=90.0)
    for i in range(50):                                     # straighten out -> commit
      scc.observe_frame(t + 4.0 + i * 0.01, 12.0, 0.0, 0.0, 0.0, True, False, False,
                        False, False, 3.0, brake_pressed=True, lat=37.5, lon=-122.0, bearing=90.0)
    c = next(iter(store.corners.values()))
    assert c.a_hi >= hi_before - 1e-9, "a braked pass may not lower an orphan's ceiling"


class TestWhatThePillsAreTold:
  """v3.7.1 — the onroad MAP and LRN pills read `longitudinalPlanSP` and
  fp_learn, and both were being told something other than what the car was
  doing. The SP planner imports cereal and cannot be constructed here, so the
  publish is pinned on the AST like every other planner guard in this repo."""

  def _fn(self, name):
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "longitudinal_planner.py"
    tree = ast.parse(src.read_text())
    return ast.unparse(next(n for n in ast.walk(tree)
                            if isinstance(n, ast.FunctionDef) and n.name == name))

  def test_the_map_pill_is_told_the_gated_cap_not_the_raw_ask(self):
    """MUTATION: publish `_scc_map_v2.output_v_target` in `sccMap.vTarget`
    again and the pill lights while scc_fusion has vetoed the cap."""
    pub = self._fn("publish_longitudinal_plan_sp")
    line = next(ln for ln in pub.splitlines() if "sccMap.vTarget =" in ln)
    assert "_scc_map_v2.output_v_target" not in line, line
    assert "_v_scc_map_gated" in line
    active = next(ln for ln in pub.splitlines() if "sccMap.active =" in ln)
    assert "_v_scc_map_gated" in active

  def test_the_gated_cap_is_the_one_the_governor_was_handed(self):
    """Anti-vacuous: the attribute is assigned FROM the fusion's output inside
    update_targets, after gate_map_target has run."""
    upd = self._fn("update_targets")
    assert upd.index("gate_map_target(") < upd.index("self._v_scc_map_gated = ")
    line = next(ln for ln in upd.splitlines() if "self._v_scc_map_gated = " in ln)
    assert "v_scc_map" in line

  def test_the_lrn_pill_lights_only_for_a_learned_governing_corner(self):
    """MUTATION: pass `is_active` alone to write_learn_shm and LRN lights for
    every SCC-M cap, learned or not — and disagrees with the minimap's own LRN
    (fp_scc's `learned`, which is `gov_confidence > 0`)."""
    import ast
    tree = ast.parse(self._fn("publish_longitudinal_plan_sp"))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func).endswith("write_learn_shm"))
    second = ast.unparse(call.args[1])
    assert "is_active" in second and "gov_confidence" in second, second
