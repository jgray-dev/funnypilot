"""FunnyPilot v3.5.0 — the onroad HUD's decision logic.

Only the pure parts are tested: the drawing needs a GL context, so everything
that decides WHAT to draw is factored out of the code that draws it. That split
is not a testing convenience — it is the reason a bad readout can be caught
here instead of on a moving car.

Covered:
  * the minimap's colour ramp (how far under the posted limit -> tint)
  * the minimap's ego-frame projection (GPS -> forward/right metres)
  * the speed-limit halo schedule (direction and proximity -> colour, weight)
  * safe_draw, which is the thing standing between a broken widget and a
    device that will not show you its settings screen
"""
import math

import pytest

from openpilot.selfdrive.ui.sunnypilot.onroad.tests.test_hud_imports import _load

_rm = _load('route_map')
_ss = _load('speed_sign')
_tok = _load('tokens')
_ch = _load('chrome')
_st = _load('stations')

ramp_color = _rm.ramp_color
to_ego_frame = _rm.to_ego_frame
bearing_lerp = _rm.bearing_lerp
edge_fade = _rm.edge_fade
halo_spec = _ss.halo_spec


class _Rect:
  """Stand-in for rl.Rectangle — pyray is stubbed in this harness."""
  def __init__(self, x, y, w, h):
    self.x, self.y, self.width, self.height = x, y, w, h


class TestBearingLerp:
  """v3.5.1. The map's pose is eased between 1 Hz fixes; a heading that eases
  the long way round spins the whole route through 358 degrees on a wrap."""

  def test_it_takes_the_short_way_across_north(self):
    out = bearing_lerp(359.0, 1.0, 0.5)
    assert out == pytest.approx(0.0, abs=1e-6) or out == pytest.approx(360.0, abs=1e-6)

  def test_it_takes_the_short_way_the_other_direction(self):
    assert bearing_lerp(1.0, 359.0, 0.5) == pytest.approx(0.0, abs=1e-6)

  def test_alpha_zero_holds_and_alpha_one_arrives(self):
    assert bearing_lerp(90.0, 200.0, 0.0) == pytest.approx(90.0)
    assert bearing_lerp(90.0, 200.0, 1.0) == pytest.approx(200.0)

  def test_output_is_always_a_heading(self):
    for cur in (0.0, 90.0, 180.0, 270.0, 359.9):
      for tgt in (0.0, 45.0, 179.0, 181.0, 359.0):
        out = bearing_lerp(cur, tgt, 0.3)
        assert 0.0 <= out < 360.0


class TestEdgeFade:
  """v3.5.1. The road already driven used to blink out of existence the instant
  it crossed the box boundary. There is no scissor available here, so the fade
  reaching zero BEFORE the edge is also what keeps the ribbon in its box."""

  def test_well_inside_is_untouched(self):
    r = _Rect(0, 0, 200, 400)
    assert edge_fade(100, 200, r) == 1.0

  def test_it_is_zero_at_every_edge(self):
    r = _Rect(0, 0, 200, 400)
    assert edge_fade(0, 200, r) == 0.0
    assert edge_fade(200, 200, r) == 0.0
    assert edge_fade(100, 0, r) == 0.0
    assert edge_fade(100, 400, r) == 0.0

  def test_it_is_zero_outside(self):
    r = _Rect(0, 0, 200, 400)
    assert edge_fade(-50, 200, r) == 0.0
    assert edge_fade(100, 900, r) == 0.0

  def test_it_ramps_monotonically_inward(self):
    r = _Rect(0, 0, 200, 400)
    vals = [edge_fade(100, y, r) for y in range(int(_rm.FADE_PX) + 1)]
    assert vals == sorted(vals)
    assert vals[-1] == pytest.approx(1.0)

  def test_it_respects_the_rect_origin(self):
    """A rect that does not start at 0,0 is the normal case on this screen."""
    r = _Rect(1000, 500, 200, 400)
    assert edge_fade(1100, 700, r) == 1.0
    assert edge_fade(1000, 700, r) == 0.0


MPH = 1.0 / 2.23694   # mph -> m/s
expected_speed_at = _rm.expected_speed_at


class TestExpectedSpeedAt:
  """v3.5.2. The minimap's tint used to be measured against the POSTED LIMIT,
  which answers the wrong question: a 35 mph curve in a 55 zone glowed red even
  when your set speed was 45 and you were only ever going to drop 10.

  It is now measured against the speed we expect to be doing AT THAT POINT.
  """

  def test_the_reported_case_set_speed_below_the_limit(self):
    """35 curve, 55 zone, set speed 45 -> a 10 mph drop, not 20."""
    ref = 45 * MPH
    exp = expected_speed_at(ref, 55 * MPH, 0.0, sla_active=False)
    delta_mph = (exp - 35 * MPH) * 2.23694
    assert delta_mph == pytest.approx(10.0, abs=0.1)
    assert ramp_color(delta_mph) != ramp_color(20.0), "must not read like the old 20 mph delta"

  def test_sla_lowers_the_reference_at_the_corner(self):
    """The corner sits in a slower zone SLA will have walked us down into, so
    the comparison happens at the reduced speed — the drop you will actually
    feel, not the one measured from here."""
    ref = 55 * MPH
    free = expected_speed_at(ref, 35 * MPH, 0.0, sla_active=False)
    with_sla = expected_speed_at(ref, 35 * MPH, 0.0, sla_active=True)
    assert free == pytest.approx(ref)
    assert with_sla == pytest.approx(35 * MPH)
    assert with_sla < free

  def test_sla_offset_ratio_is_honoured(self):
    """A carried +20% means SLA settles 20% over the sign, not on it."""
    out = expected_speed_at(60 * MPH, 40 * MPH, 0.20, sla_active=True)
    assert out == pytest.approx(48 * MPH, rel=1e-6)

  def test_it_only_ever_lowers_the_reference(self):
    """MUTATION: max() instead of min(). A driver's +50% offset must never be
    able to raise the expected speed above a set speed they chose."""
    ref = 45 * MPH
    out = expected_speed_at(ref, 55 * MPH, 0.50, sla_active=True)
    assert out == pytest.approx(ref)

  def test_sla_off_ignores_the_zone_entirely(self):
    """Nothing is going to slow us for a sign we are not obeying."""
    assert expected_speed_at(55 * MPH, 25 * MPH, 0.0, False) == pytest.approx(55 * MPH)

  def test_no_zone_data_falls_back_to_the_reference(self):
    for lim in (0.0, -1.0, float('nan'), float('inf')):
      assert expected_speed_at(50 * MPH, lim, 0.0, True) == pytest.approx(50 * MPH)

  def test_a_garbage_reference_yields_nothing_rather_than_a_wild_tint(self):
    for ref in (0.0, -5.0, float('nan'), float('inf')):
      assert expected_speed_at(ref, 30 * MPH, 0.0, True) == 0.0

  def test_a_nonsense_ratio_cannot_invert_the_speed(self):
    """A ratio below -100% would make the expected speed negative, which would
    paint the whole route red."""
    assert expected_speed_at(50 * MPH, 30 * MPH, -5.0, True) > 0.0


class TestRampColor:
  """The tint answers 'how much slower than the posted limit does the map want
  us here', not 'how fast'. A 35 mph curve in a 35 zone must look like nothing."""

  def test_no_delta_is_neutral(self):
    assert ramp_color(0.0) == _rm._RAMP[0][1]

  def test_small_delta_still_neutral(self):
    """MUTATION: drop DELTA_LO to 0. Every rounding wobble in mapd's speeds
    would then paint the whole route amber."""
    assert ramp_color(_rm.DELTA_LO - 0.5) == _rm._RAMP[0][1]

  def test_saturates_at_the_top(self):
    assert ramp_color(_rm.DELTA_HI) == _rm._RAMP[-1][1]
    assert ramp_color(_rm.DELTA_HI * 4) == _rm._RAMP[-1][1]

  def test_red_channel_is_monotone_in_delta(self):
    prev = -1
    for d in range(0, 40, 2):
      r = ramp_color(float(d))[0]
      assert r >= prev - 1
      prev = r

  def test_the_users_worked_example(self):
    """35 in a 35 zone: nothing. 35 in a 55 zone: unmistakably hot."""
    quiet = ramp_color(0.0)
    loud = ramp_color(20.0)
    assert loud[0] > quiet[0] + 60      # much more red
    assert loud[2] < quiet[2] - 60      # much less blue

  def test_non_finite_is_neutral_not_alarming(self):
    """Garbage in the map data must read as 'nothing here', never as a red
    corner. Failing toward the alarming colour would make bad OSM data look
    exactly like the thing this widget exists to detect."""
    for junk in (float('nan'), float('inf'), float('-inf')):
      assert ramp_color(junk) == _rm._RAMP[0][1]


class TestEgoFrame:
  """Forward must be up the screen and right must be right, at any heading.
  Getting the rotation sign wrong mirrors every corner — which would look
  plausible and be completely wrong."""

  def test_a_point_due_north_while_heading_north_is_straight_ahead(self):
    fwd, right = to_ego_frame(10.0009, 20.0, 10.0, 20.0, 0.0)
    assert fwd > 90 and fwd < 110
    assert abs(right) < 1.0

  def test_heading_east_puts_north_on_the_left(self):
    fwd, right = to_ego_frame(10.0009, 20.0, 10.0, 20.0, 90.0)
    assert abs(fwd) < 1.0
    assert right < -90          # north is to our left when we face east

  def test_heading_north_puts_east_on_the_right(self):
    fwd, right = to_ego_frame(10.0, 20.0009, 10.0, 20.0, 0.0)
    assert abs(fwd) < 1.0
    assert right > 80

  def test_distance_is_preserved_under_rotation(self):
    """MUTATION: mix up sin/cos in the rotation. Range would then depend on
    which way the car happens to be pointing."""
    base = None
    for bearing in (0.0, 37.0, 90.0, 180.0, 271.0, 359.0):
      fwd, right = to_ego_frame(10.0007, 20.0004, 10.0, 20.0, bearing)
      d = math.hypot(fwd, right)
      if base is None:
        base = d
      assert abs(d - base) < 0.5

  def test_ego_is_the_origin(self):
    fwd, right = to_ego_frame(10.0, 20.0, 10.0, 20.0, 123.0)
    assert abs(fwd) < 1e-6 and abs(right) < 1e-6


class TestHaloSpec:
  """The sign's halo is the whole no-text alert system. Direction is colour,
  proximity is weight."""

  def test_no_sla_and_no_change_is_no_halo(self):
    color, prox = halo_spec(0, 65, 0, sla_active=False)
    assert color is None and prox == 0.0

  def test_sla_active_and_nothing_pending_is_the_quiet_cyan(self):
    color, prox = halo_spec(0, 65, 0, sla_active=True)
    assert color is _ss.CYAN
    assert prox == _ss.HALO_IDLE

  def test_lower_zone_is_red(self):
    color, _ = halo_spec(45, 65, 200, sla_active=True)
    assert color is _ss.RED

  def test_higher_zone_is_green(self):
    color, _ = halo_spec(65, 45, 200, sla_active=True)
    assert color is _ss.GREEN

  def test_weight_grows_as_the_boundary_closes(self):
    """MUTATION: make proximity constant. 'A change is coming' and 'how soon'
    collapse into one bit and the driver loses the timing."""
    prev = -1.0
    for d in (400, 300, 200, 100, 20, 0):
      _, prox = halo_spec(45, 65, float(d), sla_active=True)
      assert prox >= prev
      prev = prox
    assert prev > 0.95

  def test_distant_change_is_still_visible(self):
    """A confirmed zone at the edge of the envelope must not be invisible —
    HALO_MIN is what stops it fading to nothing."""
    _, prox = halo_spec(45, 65, 10_000.0, sla_active=True)
    assert prox >= _ss.HALO_MIN

  def test_a_change_of_less_than_one_unit_is_not_a_change(self):
    """Rounding in the resolver must not flash the halo."""
    color, _ = halo_spec(65.4, 65.0, 100, sla_active=False)
    assert color is None

  def test_non_finite_distance_degrades_to_far(self):
    _, prox = halo_spec(45, 65, float('nan'), sla_active=True)
    assert prox == _ss.HALO_MIN

  def test_halo_works_without_sla(self):
    """The sign warns about the zone change whether or not SLA is acting on
    it — the halo is information, not a feature indicator."""
    color, prox = halo_spec(45, 65, 150, sla_active=False)
    assert color is _ss.RED and prox > _ss.HALO_MIN


class TestSafeDraw:
  """The blast shield. Read the tokens.py docstring: an unguarded raise in an
  onroad widget is a UI boot-loop, and a device with no UI cannot be flashed
  from its own settings screen."""

  def setup_method(self):
    _tok.reset_disabled()

  def teardown_method(self):
    _tok.reset_disabled()

  def test_a_working_widget_runs_every_time(self):
    calls = []
    for _ in range(5):
      _tok.safe_draw("ok", lambda: calls.append(1))
    assert len(calls) == 5

  def test_arguments_are_passed_through(self):
    seen = []
    _tok.safe_draw("args", lambda a, b=None: seen.append((a, b)), 1, b=2)
    assert seen == [(1, 2)]

  def test_an_exception_does_not_propagate(self):
    def boom():
      raise ValueError("bad frame")
    _tok.safe_draw("boom", boom)     # must not raise

  def test_a_failed_widget_is_disabled_for_the_session(self):
    """MUTATION: catch and continue without disabling. The widget would then
    raise at 60 Hz, filling the log and burning the frame budget the rest of
    the UI needs."""
    calls = []

    def boom():
      calls.append(1)
      raise ValueError

    for _ in range(10):
      _tok.safe_draw("boom", boom)
    assert len(calls) == 1
    assert _tok.is_disabled("boom")

  def test_one_bad_widget_does_not_disable_the_others(self):
    ok = []
    _tok.safe_draw("bad", lambda: (_ for _ in ()).throw(ValueError()))
    for _ in range(3):
      _tok.safe_draw("good", lambda: ok.append(1))
    assert len(ok) == 3
    assert _tok.is_disabled("bad") and not _tok.is_disabled("good")


class TestTokenHelpers:
  def test_clamp(self):
    assert _tok.clamp(5, 0, 1) == 1
    assert _tok.clamp(-5, 0, 1) == 0
    assert _tok.clamp(0.5, 0, 1) == 0.5

  def test_smoothstep_is_eased_at_both_ends(self):
    assert _tok.smoothstep(0.0) == 0.0
    assert _tok.smoothstep(1.0) == 1.0
    assert _tok.smoothstep(0.5) == 0.5
    assert _tok.smoothstep(0.1) < 0.1      # eases in
    assert _tok.smoothstep(0.9) > 0.9      # eases out

  def test_finite_rejects_junk(self):
    assert _tok.finite(1.0)
    assert not _tok.finite(float('nan'))
    assert not _tok.finite(float('inf'))
    assert not _tok.finite("42")
    assert not _tok.finite(None)


class TestEased:
  """v3.5.4. One time constant for the whole screen — see tokens.EASE_TAU."""

  def test_it_moves_toward_the_target(self):
    e = _tok.Eased(0.0)
    e.update(1.0, now=0.0)
    a = e.update(1.0, now=0.05)
    b = e.update(1.0, now=0.10)
    assert 0.0 < a < b < 1.0

  def test_it_settles_exactly(self):
    """EASE_SNAP exists so a value can actually REACH its target. An asymptote
    that never arrives leaves a pill at 99% alpha forever."""
    e = _tok.Eased(0.0)
    t = 0.0
    for _ in range(400):
      t += 0.016
      e.update(1.0, now=t)
    assert e.x == 1.0

  def test_it_is_frame_rate_independent(self):
    """MUTATION: use a fixed per-frame fraction instead of exp(-dt/tau). The
    feel would then change whenever the frame rate moves — a bug that only
    shows up when the device is hot and throttling."""
    fast, slow = _tok.Eased(0.0), _tok.Eased(0.0)
    t = 0.0
    for _ in range(60):          # 120 fps
      t += 1 / 120.0
      fast.update(1.0, now=t)
    t = 0.0
    for _ in range(30):          # 60 fps, same half-second
      t += 1 / 60.0
      slow.update(1.0, now=t)
    assert abs(fast.x - slow.x) < 0.02

  def test_a_stalled_frame_does_not_teleport(self):
    e = _tok.Eased(0.0)
    e.update(1.0, now=0.0)
    e.update(1.0, now=30.0)      # 30 s gap: a paused/backgrounded UI
    assert e.x < 1.0

  def test_snap_is_immediate(self):
    e = _tok.Eased(0.0)
    assert e.snap(0.7) == 0.7 and e.x == 0.7

  def test_a_non_finite_target_is_ignored(self):
    e = _tok.Eased(0.5)
    e.update(float('nan'), now=0.0)
    e.update(float('inf'), now=0.1)
    assert e.x == 0.5


class TestChromeScale:
  """v3.5.4. The vignette and bands were tuned for daylight and applied at full
  strength regardless; at night that is far heavier than the scene needs."""

  def test_full_chrome_in_daylight(self):
    assert _ch.chrome_scale(100.0) == 1.0

  def test_reduced_in_the_dark(self):
    assert _ch.chrome_scale(0.0) < 1.0

  def test_it_is_floored_well_above_zero(self):
    """LOAD-BEARING: the vignette is what stops the state glow washing out
    (v3.5.0). Scaling it to nothing at night trades one failure for another.
    MUTATION: drop CHROME_MIN and let it reach 0."""
    for pct in (-50.0, 0.0, 1.0):
      assert _ch.chrome_scale(pct) >= _ch.CHROME_MIN
    assert _ch.CHROME_MIN > 0.4

  def test_it_is_monotone_and_bounded(self):
    vals = [_ch.chrome_scale(p) for p in (0, 10, 25, 50, 75, 100, 400)]
    assert vals == sorted(vals)
    assert all(_ch.CHROME_MIN <= v <= 1.0 for v in vals)

  def test_garbage_reads_as_daylight(self):
    """An unreadable sensor must degrade to TODAY's behaviour — full chrome is
    the daylight-safe answer, and guessing 'dark' would dim the vignette in
    exactly the conditions it is needed."""
    for junk in (None, "x", float('nan')):
      assert _ch.chrome_scale(junk) == 1.0


def rgba(c):
  """Compare colours BY VALUE, not identity: `_load` imports each module
  independently, so `speed_sign`'s tokens and `_tok` are separate objects even
  when the source is shared. Value equality is also the stronger assertion —
  it is what 'one palette' actually means."""
  return (c.r, c.g, c.b, c.a)


class TestLongDotColor:
  def test_states_map_to_the_shared_palette(self):
    assert rgba(_st.long_dot_color("green")) == rgba(_tok.ENGAGED)
    assert rgba(_st.long_dot_color("red")) == rgba(_tok.HALT)
    assert rgba(_st.long_dot_color("gray")) == rgba(_st.DOT_IDLE)
    assert rgba(_st.long_dot_color("nonsense")) == rgba(_st.DOT_IDLE)


class TestOnePalette:
  """v3.5.4. speed_sign.py had declared RED/GREEN/CYAN as byte-identical copies
  of three tokens. tokens.py exists to stop exactly that drift, and it had
  already happened once."""

  def test_the_sign_uses_the_shared_colours(self):
    assert rgba(_ss.RED) == rgba(_tok.HALT)
    assert rgba(_ss.GREEN) == rgba(_tok.ENGAGED)
    assert rgba(_ss.CYAN) == rgba(_tok.LAT_ONLY)

  def test_the_sign_declares_no_colours_of_its_own(self):
    """MUTATION: paste a literal rl.Color back into speed_sign.py. Value
    equality alone would still pass if someone re-typed the same hex, so this
    checks the SOURCE — the drift is the problem, not the current value."""
    import ast
    import pathlib
    src = pathlib.Path(_ss.__file__).read_text()
    literals = [
      n for n in ast.walk(ast.parse(src))
      if isinstance(n, ast.Call) and ast.unparse(n.func) in ("rl.Color", "pyray.Color")
      and any(isinstance(a, ast.Constant) for a in n.args)
    ]
    assert not literals, (
      "speed_sign.py must take its colours from tokens.py: " +
      ", ".join(ast.unparse(n) for n in literals)
    )


class TestLaneOffset:
  """v3.5.5. mapd's route points are the OSM way — the road CENTRELINE — while
  the GPS fix is the car, in the right-hand lane. The marker is drawn at the
  projection origin, so the ribbon landed beside it. Reported as "the position
  marker is offset to the right of the minimap road line"."""

  @staticmethod
  def _straight(right, n=8, spacing=20.0, start=-40.0):
    return [(start + i * spacing, right, 20.0, 25.0) for i in range(n)]

  def test_it_finds_the_offset_where_we_actually_are(self):
    assert _rm.lateral_offset_at_ego(self._straight(3.0)) == pytest.approx(3.0)

  def test_it_interpolates_rather_than_snapping_to_a_point(self):
    """MUTATION: use the nearest point instead of interpolating. The value then
    STEPS every time the nearest index changes, which is precisely the 1 Hz
    jitter v3.5.1 went to some trouble to remove."""
    pts = [(-10.0, 0.0, 20.0, 25.0), (10.0, 4.0, 20.0, 25.0)]
    assert _rm.lateral_offset_at_ego(pts) == pytest.approx(2.0)

  def test_the_shift_removes_it_exactly(self):
    pts = self._straight(3.2)
    shift = _rm.lateral_offset_at_ego(pts)
    assert all(abs(r - shift) < 1e-9 for _f, r, _v, _l in pts)

  def test_an_implausible_offset_is_refused(self):
    """BOUNDED: a bad route match must be allowed to look wrong rather than be
    allowed to drag the whole ribbon somewhere it does not belong.
    MUTATION: drop LANE_SHIFT_MAX_M."""
    assert _rm.lateral_offset_at_ego(self._straight(400.0)) == 0.0
    assert _rm.lateral_offset_at_ego(self._straight(-400.0)) == 0.0
    assert _rm.LANE_SHIFT_MAX_M < 20.0

  def test_a_route_entirely_ahead_still_answers(self):
    pts = [(20.0, 2.5, 20.0, 25.0), (40.0, 2.6, 20.0, 25.0)]
    assert _rm.lateral_offset_at_ego(pts) == pytest.approx(2.5)

  def test_garbage_is_no_shift_at_all(self):
    assert _rm.lateral_offset_at_ego([]) == 0.0
    assert _rm.lateral_offset_at_ego([(0.0, float('nan'), 20.0, 25.0)]) == 0.0


class TestStitchToEgo:
  """v3.5.5. mapd publishes the route from its matched position forward, so
  after a re-match the first point can be well ahead of us and nothing joins it
  to the car. Reported as "a gap between the road ahead and my marker"."""

  def test_a_route_starting_ahead_is_joined_to_the_car(self):
    pts = [(30.0, 0.0, 18.0, 25.0), (60.0, 0.0, 18.0, 25.0)]
    out = _rm.stitch_to_ego(pts)
    assert len(out) == len(pts) + 1
    assert out[0][0] == 0.0 and out[0][1] == 0.0
    assert out[1:] == pts          # never moves or drops a real point

  def test_it_carries_the_first_points_speed_and_zone(self):
    """The stitched segment must be tinted like the road it leads to, or the
    join reads as a different piece of road."""
    pts = [(30.0, 0.0, 18.0, 25.0)]
    assert _rm.stitch_to_ego(pts)[0][2:] == (18.0, 25.0)

  def test_a_route_that_already_covers_us_is_untouched(self):
    pts = [(-20.0, 0.0, 18.0, 25.0), (20.0, 0.0, 18.0, 25.0)]
    assert _rm.stitch_to_ego(pts) is pts

  def test_a_distant_route_is_not_invented(self):
    """BOUNDED: past STITCH_MAX_M a straight segment would be fiction, and
    drawing geometry the controller cannot see is the one thing this widget
    exists not to do. MUTATION: remove the bound."""
    pts = [(300.0, 0.0, 18.0, 25.0)]
    assert _rm.stitch_to_ego(pts) is pts
    assert _rm.STITCH_MAX_M <= 100.0

  def test_empty_is_safe(self):
    assert _rm.stitch_to_ego([]) == []


class TestZoneChange:
  """v3.5.6. The ribbon has carried the zone limit at every point since v3.5.2
  and nothing read it; `zone_change` is what finally draws the boundary."""

  @staticmethod
  def _route(limits, start=-40.0, step=20.0):
    return [(start + i * step, 0.0, 20.0, lim) for i, lim in enumerate(limits)]

  def test_no_change_reads_as_none(self):
    assert _rm.zone_change(self._route([20.0] * 6)) is None

  def test_it_finds_the_first_boundary_ahead(self):
    pts = self._route([20.0, 20.0, 20.0, 11.2, 11.2])
    zc = _rm.zone_change(pts)
    assert zc is not None
    i, new_lim, old_lim = zc
    assert pts[i][0] > 0.0, "the boundary must be AHEAD of us, not behind"
    assert (new_lim, old_lim) == (11.2, 20.0)

  def test_the_reference_zone_is_the_one_we_are_in(self):
    """MUTATION: take the base limit from pts[0]. That point is BEHIND the car
    (BEHIND_M keeps ~170 m of trail), so a boundary already crossed would be
    re-reported as one still ahead."""
    pts = self._route([11.2, 11.2, 20.0, 20.0, 20.0], start=-40.0)
    zc = _rm.zone_change(pts)
    assert zc is None, "a boundary we have already driven through is not ahead"

  def test_a_faster_zone_is_reported_as_such(self):
    """The caller colours on new < old, so the ORDER of the pair is the whole
    contract — swapping it turns every red gate green."""
    _i, new_lim, old_lim = _rm.zone_change(self._route([11.2, 11.2, 11.2, 20.0, 20.0]))
    assert (new_lim, old_lim) == (20.0, 11.2)

  def test_missing_limits_are_ignored(self):
    assert _rm.zone_change([]) is None
    assert _rm.zone_change(self._route([0.0, 0.0, 0.0])) is None

  def test_noise_is_not_a_boundary(self):
    """Zone limits arrive as floats; a fractional wobble is not a new zone."""
    assert _rm.zone_change(self._route([20.0, 20.0, 20.1, 20.0])) is None


class TestTintIsVisible:
  """v3.5.6. Reported as "white 99% of the time": full red needed 25 mph under
  the expected speed, so an ordinary corner rendered as barely-tinted grey."""

  @staticmethod
  def _coloured(c):
    """Reads as a warning colour rather than as road."""
    return c[0] > 200 and c[0] - c[2] > 60

  def test_an_ordinary_corner_is_actually_coloured(self):
    """MUTATION: put DELTA_HI back to 25. A 4 mph trim then sits at t = 0.04
    and is indistinguishable from a straight road."""
    assert not self._coloured(_rm.ramp_color(0.0)), "flat road must stay neutral"
    for delta in (4.0, 6.0, 8.0, 10.0):
      assert self._coloured(_rm.ramp_color(delta)), f"{delta} mph under must show"

  def test_it_saturates_at_a_plausible_corner(self):
    assert _rm.DELTA_HI <= 15.0, "full red must be reachable by a real corner"
    assert _rm.ramp_color(_rm.DELTA_HI) == _rm.ramp_color(40.0)

  def test_the_ramp_is_monotone_toward_red(self):
    reds = [_rm.ramp_color(d)[0] for d in (0, 2, 4, 6, 8, 10, 13, 20)]
    assert reds == sorted(reds)
    greens = [_rm.ramp_color(d)[1] for d in (4, 6, 8, 10, 13, 20)]
    assert greens == sorted(greens, reverse=True)

  def test_the_neutral_is_not_white(self):
    """It is the ROAD. It was competing with the white ego marker and text."""
    r, g, b = _rm.ramp_color(0.0)
    assert max(r, g, b) < 180 and b > r


class TestTheTrailOutlivesTheScreen:
  """v3.5.6. Reported: ~150 px of already-driven road vanishing while still on
  screen. Arithmetic, not taste -- see BEHIND_M."""

  H = 1020.0   # onroad content height

  def _geom(self):
    scale = self.H * (_rm.EGO_FROM_BOTTOM - 0.04) / _rm.RANGE_M
    below_px = self.H * (1.0 - _rm.EGO_FROM_BOTTOM)
    return scale, below_px

  def test_the_tail_reaches_past_the_bottom_edge(self):
    """MUTATION: put BEHIND_M back to 90. It is then FIVE PIXELS short of the
    visible area before any lag at all."""
    scale, below_px = self._geom()
    assert _rm.BEHIND_M * scale > below_px

  def test_it_survives_a_poll_interval_of_travel_plus_pose_lag(self):
    """The filter trims from the pose of THAT INSTANT while the car keeps
    moving, and the displayed pose lags the polled one by POSE_TAU. Both eat
    into the tail, and together they are what made the gap visible."""
    scale, below_px = self._geom()
    worst_lag_m = (_rm.POLL_S + _rm.POSE_TAU) * 30.0   # 30 m/s
    assert _rm.BEHIND_M * scale - below_px > worst_lag_m * scale

  def test_the_fade_is_what_ends_the_ribbon(self):
    """Removal must be done by the edge fade, which knows where the screen is,
    not by a distance filter, which does not."""
    rect = _Rect(0.0, 0.0, 240.0, self.H)
    assert _rm.edge_fade(120.0, self.H, rect) == 0.0          # at the edge
    assert _rm.edge_fade(120.0, self.H + 50.0, rect) == 0.0    # past it
    assert _rm.edge_fade(120.0, self.H - 1.0, rect) < 0.1      # essentially gone
    assert _rm.edge_fade(120.0, self.H / 2.0, rect) == 1.0     # well inside


class TestCornerSpeedAt:
  """FunnyPilot v3.6.2 — the ribbon's tint now comes from the speed SCC-M v2
  CHOSE for each corner, published over /dev/shm/fp_corners, not from mapd's
  own `velocity` field. The UI cannot compute those speeds: half of each one
  is a learned lateral budget in a store on /data, and nothing in hud/ may
  touch a filesystem.

  `0.0` means NO CORNER HERE, which is a real answer — most of any route is
  straight — and the renderer draws it as untinted road.
  """

  def _corner(self, lat=37.5, lon=-122.0, half=50.0, v=12.0, conf=0.5):
    return (lat, lon, half, v, conf)

  def test_a_point_inside_a_corner_gets_its_speed(self):
    c = self._corner()
    assert _rm.corner_speed_at(37.5, -122.0, [c]) == pytest.approx(12.0)

  def test_a_point_outside_every_corner_gets_nothing(self):
    """MUTATION: return the nearest corner's speed regardless of distance. Every
    straight road would then be tinted for a bend somewhere else on the route."""
    c = self._corner(half=50.0)
    far = 37.5 + 500.0 / 111320.0
    assert _rm.corner_speed_at(far, -122.0, [c]) == 0.0

  def test_the_slower_of_two_overlapping_corners_wins(self):
    """The same min() the controller takes, over the same list, so the ribbon
    cannot show a corner the cap is not honouring."""
    a = self._corner(v=18.0, half=80.0)
    b = self._corner(v=11.0, half=80.0)
    assert _rm.corner_speed_at(37.5, -122.0, [a, b]) == pytest.approx(11.0)

  def test_an_empty_list_is_no_corner_anywhere(self):
    assert _rm.corner_speed_at(37.5, -122.0, []) == 0.0

  def test_a_zero_speed_corner_is_ignored(self):
    """A published zero is 'no speed', not 'stop here'. Treating it as a target
    would paint the road full red for a corner nothing asked for."""
    assert _rm.corner_speed_at(37.5, -122.0, [self._corner(v=0.0)]) == 0.0

  def test_the_extent_is_used_not_a_fixed_radius(self):
    """A long sweeper must tint along its whole length, and a short bend must
    not bleed into the straight after it."""
    long_bend = self._corner(half=200.0, v=20.0)
    short = self._corner(half=20.0, v=20.0)
    p = 37.5 + 120.0 / 111320.0
    assert _rm.corner_speed_at(p, -122.0, [long_bend]) == pytest.approx(20.0)
    assert _rm.corner_speed_at(p, -122.0, [short]) == 0.0


class TestTintDelta:
  """v3.6.2. A mutation that dropped the no-corner sentinel survived the whole
  suite, because the expression lived inside `render()` and nothing off-device
  can reach a draw path. It is a pure function now, and this is why."""

  def test_a_slower_corner_reads_as_a_positive_delta(self):
    assert _rm.tint_delta_mph(25.0, 15.0) == pytest.approx(10.0 * 2.23694)

  def test_no_corner_here_is_no_tint(self):
    """MUTATION: drop the `corner_mps > 0` guard. Every straight road on the
    route would then be painted full red for a corner that is not there."""
    assert _rm.tint_delta_mph(25.0, 0.0) == 0.0
    assert _rm.tint_delta_mph(25.0, -1.0) == 0.0
    assert _rm.ramp_color(_rm.tint_delta_mph(25.0, 0.0)) == _rm._RAMP[0][1]

  def test_no_reference_is_no_tint(self):
    assert _rm.tint_delta_mph(0.0, 15.0) == 0.0

  def test_a_corner_faster_than_expected_is_not_tinted_red(self):
    assert _rm.tint_delta_mph(15.0, 25.0) < 0.0
    assert _rm.ramp_color(_rm.tint_delta_mph(15.0, 25.0)) == _rm._RAMP[0][1]

  def test_garbage_is_neutral_rather_than_wild(self):
    for e, c in ((float('nan'), 15.0), (25.0, float('nan')),
                 (float('inf'), 15.0), (25.0, float('inf'))):
      assert _rm.tint_delta_mph(e, c) == 0.0


class TestLearnedCornersFrom:
  """v3.6.2. Whether to draw a ring is an EXISTENCE question (have we been
  here), answered by the visit count -- NOT by the settled confidence, which
  answers the separate question of how solid to draw it. Filtering on the
  wrong one hides every corner during exactly the first few passes a driver
  most wants to watch.

  The filter direction is also the whole safety property: this repo has
  shipped the flipped version of this kind of test before (LearnStore.nearby's
  ahead_only bug, v3.6.2) with every other test in the suite still green,
  because the bug lived one layer past what could be reached off-device.
  Pulling it out of _poll() is what makes it reachable.
  """

  def _c(self, lat=1.0, lon=2.0, half=50.0, v=12.0, settled=0.0, visits=0):
    return (lat, lon, half, v, settled, visits)

  def test_a_driven_corner_survives(self):
    out = _rm.learned_corners_from([self._c(settled=0.45, visits=2)])
    assert out == [(1.0, 2.0, 0.45)]

  def test_an_unvisited_corner_is_dropped(self):
    """MUTATION: flip `>= 1` to `< 1` and this is the only thing that catches
    it -- every other test in the suite passes either way."""
    assert _rm.learned_corners_from([self._c(visits=0)]) == []

  def test_a_visited_but_unsettled_corner_is_still_drawn(self):
    """THE REGRESSION THIS CLASS EXISTS FOR. A first-visit corner has settled
    confidence ~0, because it has no second pass to agree with. Filtering on
    settled -- the obvious-looking mistake -- would make every corner
    invisible until its fourth or fifth drive.

    MUTATION: filter on `c[4] > 0.0` instead of the visit count."""
    assert _rm.learned_corners_from([self._c(settled=0.0, visits=1)]) \
      == [(1.0, 2.0, 0.0)]

  def test_mixed_list_keeps_only_the_driven_ones(self):
    driven = self._c(lat=3.0, lon=4.0, settled=1.0, visits=40)
    unvisited = self._c(lat=5.0, lon=6.0, settled=0.0, visits=0)
    assert _rm.learned_corners_from([unvisited, driven]) == [(3.0, 4.0, 1.0)]

  def test_a_short_legacy_entry_draws_no_ring(self):
    """A 5-field entry (the first cut of v3.6.2 writer, or a torn upgrade) carries no visit
    count. read_corners_shm defaults it to 0, and a corner we cannot vouch for
    gets no ring -- while its SPEED still tints the ribbon, which is the
    quieter and more useful degradation."""
    assert _rm.learned_corners_from([(1.0, 2.0, 50.0, 12.0, 0.9)]) == []

  def test_an_empty_list_is_empty(self):
    assert _rm.learned_corners_from([]) == []


class TestConfidenceAlpha:
  """v3.6.2. The ring's opacity is CONVERGENCE, not attendance.

  The first cut of this used `confidence_for(visits)`, which saturates at
  three visits -- so a corner whose speed estimate was still moving 15% on its
  third pass drew a fully solid ring, claiming certainty about a number that
  was still being argued over. It is `confidence_of(visits, drift)` now, which
  additionally requires successive passes to agree.
  """

  def test_a_settled_corner_is_solid(self):
    assert _rm.confidence_alpha(1.0) == 255

  def test_a_first_visit_is_faint_but_visible(self):
    """A corner driven once has converged on nothing, so its settled
    confidence is ~0 -- but it HAS been driven, and that is worth seeing.
    SEEN_MIN_ALPHA is what says so, and 20% is the requested value.

    MUTATION: drop the floor and every corner is invisible for its first
    several drives, which reads as the feature not working at all."""
    assert _rm.confidence_alpha(0.0) == int(_rm.SEEN_MIN_ALPHA * 255)
    assert _rm.confidence_alpha(0.0) > 0

  def test_partial_confidence_lands_between(self):
    """The middle of the range has to actually vary, or the ring is a
    two-state lamp wearing a gradient's clothes."""
    faint = _rm.confidence_alpha(0.0)
    mid = _rm.confidence_alpha(0.6)
    assert faint < mid < 255
    assert mid == int(0.6 * 255)

  def test_it_is_monotone_in_confidence(self):
    vals = [_rm.confidence_alpha(c / 20.0) for c in range(21)]
    assert vals == sorted(vals)

  def test_clamped_to_the_valid_range(self):
    """A store bug that let confidence outside [0, 1] must not paint a ring
    darker than solid or invert to a negative alpha."""
    assert _rm.confidence_alpha(1.4) == 255
    assert _rm.confidence_alpha(-0.3) == int(_rm.SEEN_MIN_ALPHA * 255)

  def test_non_finite_reads_as_the_floor_not_as_certainty(self):
    """MUTATION: NaN/inf falling through to int() would either crash the
    onroad draw loop or paint a solid ring for a value that means nothing.
    It degrades to "seen, not settled", the most modest claim available."""
    for bad in (float('nan'), float('inf'), float('-inf')):
      assert _rm.confidence_alpha(bad) == int(_rm.SEEN_MIN_ALPHA * 255)


class TestLearnedCornerProjection:
  """v3.6.2 — RouteMap._project() now carries the learned-corner list through
  the same ego-frame projection and lateral shift as the ribbon itself, so a
  learned ring lands exactly on the road it belongs to rather than drifting
  off it the way the pre-shift marker would have.
  """

  def _map_at_origin(self, raw_corners, bearing=0.0):
    m = _rm.RouteMap()
    m._pose = (37.5, -122.0, bearing)
    m._raw = []
    m._raw_corners = raw_corners
    return m

  def test_a_corner_ahead_projects_to_positive_forward(self):
    """~100 m due north of a 0-degree-heading ego is ~100 m forward, 0 right."""
    m = self._map_at_origin([(37.5 + 100.0 / _rm._M_PER_DEG, -122.0, 0.6)])
    pts, corners = m._project()
    assert len(corners) == 1
    fwd, right, conf = corners[0]
    assert fwd == pytest.approx(100.0, abs=0.5)
    assert right == pytest.approx(0.0, abs=0.5)
    assert conf == pytest.approx(0.6)

  def test_confidence_passes_through_untouched(self):
    """The projection must not touch the third element -- it is the
    controller's confidence, not a screen coordinate."""
    m = self._map_at_origin([(37.5, -122.0 + 50.0 / _rm._M_PER_DEG, 1.0)])
    _, corners = m._project()
    assert corners[0][2] == pytest.approx(1.0)

  def test_no_pose_yet_is_no_corners(self):
    """Before the first GPS fix there is nothing to project onto."""
    m = _rm.RouteMap()
    m._raw_corners = [(37.5, -122.0, 0.5)]
    pts, corners = m._project()
    assert pts == [] and corners == []

  def test_no_learned_corners_is_an_empty_list_not_a_crash(self):
    m = self._map_at_origin([])
    _, corners = m._project()
    assert corners == []


_sig = _load('side_signals')


class TestSideSignals:
  """FunnyPilot v3.6.2 — the left and right edges of the state glow become the
  blinker and the blind spot.

  The behaviour that matters is not "does it light up" but the three states'
  precedence and the fact that an idle edge costs nothing at all: this runs at
  60 Hz over the camera image, and a band that asymptotes toward zero instead
  of reaching it would draw 26 gradients a frame forever.
  """

  def step(self, sig, seconds, blinker=False, blindspot=False, position=None, dt=1 / 60):
    for _ in range(int(seconds / dt)):
      sig.update(dt, blinker, blindspot, position)
    return sig

  def test_idle_costs_nothing(self):
    """MUTATION: drop the settle in `update`. An exponential approach never
    REACHES zero, so an edge that has ever been lit keeps reporting a live kind
    forever and the renderer keeps drawing 26 fully-transparent gradients a
    frame for it.

    IT MUST BE SHOWN FROM A LIT STATE. Starting from a fresh object exercises
    nothing: presence is already exactly zero and stays there whatever the
    settle does — the first version of this test did that and the mutation
    walked straight through it."""
    s = _sig.SideSignal()
    self.step(s, 1.0, blinker=True)
    assert s.kind == _sig.KIND_BLINKER
    self.step(s, 2.0)
    assert s.alpha == 0.0
    assert s.kind == _sig.KIND_NONE, "a switched-off edge still reports itself lit"

  def test_a_blinker_pulses_amber_across_the_whole_side(self):
    s = _sig.SideSignal()
    seen = []
    for _ in range(int(2.0 * 60)):
      s.update(1 / 60, True, False)
      seen.append(s.alpha)
    assert s.kind == _sig.KIND_BLINKER
    assert s.centre == 0.5 and s.extent == 1.0, "a turn signal is about the whole side"
    assert max(seen) > 0.7 and min(seen[30:]) < 0.4, "it must actually pulse"

  def test_the_pulse_is_at_the_blinker_cadence(self):
    """Not a taste value: 1.5 Hz is inside the 60-120 flashes/minute the
    regulations require, and it matches the relay the driver can hear."""
    s = _sig.SideSignal()
    prev, rises = 0.0, 0
    for _ in range(int(4.0 * 60)):
      s.update(1 / 60, True, False)
      if s.alpha > 0.8 >= prev:
        rises += 1
      prev = s.alpha
    assert 4 <= rises <= 8, f"{rises} pulses in 4 s"

  def test_the_pulse_never_goes_fully_dark(self):
    """A hard on/off at 1.5 Hz in peripheral vision is a strobe. The dip floor
    is what makes this readable rather than alarming."""
    s = _sig.SideSignal()
    lows = []
    for _ in range(int(3.0 * 60)):
      s.update(1 / 60, True, False)
      lows.append(s.alpha)
    assert min(lows[60:]) >= _sig.BLINK_MIN_A - 0.05

  def test_a_blind_spot_is_red_and_sits_below_centre(self):
    """The frame is a forward view and the zone is beside and behind the
    driver, so it belongs low on the edge."""
    s = self.step(_sig.SideSignal(), 4.0, blindspot=True)
    assert s.kind == _sig.KIND_BSD
    assert s.alpha > 0.8
    assert s.centre > 0.5, "the blind spot is not in front of you"
    assert s.extent < 1.0, "it is a place, not the whole side"

  def test_it_slides_in_rather_than_fading_up(self):
    """MUTATION: make the band appear at its final position. A lamp coming on
    reads as a warning light; something arriving reads as a car."""
    s = _sig.SideSignal()
    s.update(1 / 60, False, True)
    early = s.centre
    self.step(s, 4.0, blindspot=True)
    assert early > s.centre + 0.2, "the band did not travel"

  def test_it_slides_back_out_when_the_lane_clears(self):
    s = self.step(_sig.SideSignal(), 4.0, blindspot=True)
    settled = s.centre
    self.step(s, 4.0)
    assert s.centre > settled + 0.2
    assert s.alpha == 0.0

  def test_signalling_into_an_occupied_lane_is_red_AND_pulsing(self):
    """THE COMBINATION THAT ACTUALLY MATTERS. Red wins the colour because it is
    the hazard; the blinker wins the rhythm because it is the intent. Showing
    it as a steady red would make the one dangerous case the quietest of the
    three. MUTATION: let the blinker branch win, or drop the pulse."""
    s = _sig.SideSignal()
    self.step(s, 1.0, blinker=True, blindspot=True)
    seen = []
    for _ in range(int(2.0 * 60)):
      s.update(1 / 60, True, True)
      seen.append(s.alpha)
    assert s.kind == _sig.KIND_BSD, "the hazard must own the colour"
    assert max(seen) - min(seen) > 0.3, "and the intent must own the rhythm"

  def test_a_sensed_position_moves_the_band(self):
    """`position` is None on this car — the blind-spot signal is a boolean and
    nothing available supplies range (see the module docstring). The parameter
    exists so that a source which did would need no other change, and this
    pins that it is actually wired through rather than decorative."""
    front = self.step(_sig.SideSignal(), 4.0, blindspot=True, position=0.0)
    back = self.step(_sig.SideSignal(), 4.0, blindspot=True, position=1.0)
    assert front.centre < back.centre

  def test_garbage_never_raises_and_never_teleports(self):
    s = _sig.SideSignal()
    for dt in (0.0, -1.0, float('nan'), 10.0):
      s.update(dt, True, True, float('nan'))
      assert 0.0 <= s.alpha <= 1.0


class TestTheCornerWireFormatContract:
  """FunnyPilot v3.6.3 — THE MINIMAP DIED ON THE ROAD BECAUSE THIS DID NOT EXIST.

  v3.6.2 added a sixth field (`visits`) to /dev/shm/fp_corners so the learned
  ring could tell "never driven" from "driven once". `corner_speed_at` still
  unpacked five names, so the first corner published raised

      ValueError: too many values to unpack (expected 5)

  and `safe_draw` disabled the minimap for the rest of the session. On straight
  road the corner list is empty and the loop body never runs, so it presented
  as the map working at first and then vanishing mid-drive.

  EVERY EXISTING TEST PASSED because `TestCornerSpeedAt` builds its fixture BY
  HAND as a 5-tuple. A hand-written fixture cannot notice that the producer
  changed shape — it only ever tests the consumer against the author's memory
  of the format. These cases feed the CONSUMER with what the PRODUCER actually
  emits, so the two cannot drift again.
  """

  def _emit(self, tmp_path, monkeypatch, corners):
    """Round-trip through the real writer and reader."""
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm
    monkeypatch.setattr(scc_shm, "CORNERS_SHM_PATH", str(tmp_path / "fp_corners"))

    class _C:
      def __init__(self, lat, lon, half_len, v_target, settled, visits):
        self.lat, self.lon = lat, lon
        self.half_len, self.v_target = half_len, v_target
        self.settled, self.visits = settled, visits

    scc_shm.write_corners_shm([_C(*c) for c in corners])
    return scc_shm.read_corners_shm()

  def test_the_real_wire_format_does_not_crash_the_consumer(self, tmp_path, monkeypatch):
    """MUTATION: revert corner_speed_at to unpacking five names."""
    out = self._emit(tmp_path, monkeypatch, [(37.5, -122.0, 50.0, 12.0, 0.45, 3)])
    assert out, "the producer wrote nothing; this test would be vacuous"
    assert _rm.corner_speed_at(37.5, -122.0, out) == pytest.approx(12.0, abs=0.05)

  def test_the_learned_ring_reads_the_same_tuples(self, tmp_path, monkeypatch):
    """Both consumers of this channel are driven from one producer output, so
    a future field cannot satisfy one and break the other."""
    out = self._emit(tmp_path, monkeypatch,
                     [(37.5, -122.0, 50.0, 12.0, 0.45, 3),
                      (37.6, -122.1, 40.0, 18.0, 0.0, 0)])
    rings = _rm.learned_corners_from(out)
    assert len(rings) == 1                      # only the driven one
    assert rings[0][2] == pytest.approx(0.45)

  def test_a_further_widened_entry_still_works(self):
    """The point of indexing rather than unpacking: this consumer must be
    indifferent to fields added for somebody else's reader."""
    seven = (37.5, -122.0, 50.0, 12.0, 0.45, 3, 999.0)
    assert _rm.corner_speed_at(37.5, -122.0, [seven]) == pytest.approx(12.0)

  def test_a_short_entry_is_skipped_not_fatal(self):
    assert _rm.corner_speed_at(37.5, -122.0, [(37.5, -122.0)]) == 0.0


class TestTheSlowdownGradient:
  """FunnyPilot v3.6.4 — the ribbon shows the PLAN, not the corner's footprint.

  Hue still comes from the corner's full drop; opacity is how much of that drop
  has already happened at each point. Going in that is SCC-M v2's own
  approach_cap, coming out it is CurveSpeedCap's release ramp, so the ribbon
  darkens as the car sheds speed and fades where authority returns to the set
  speed.
  """
  MPH = 2.23694

  HALF = 20.0     # a 40 m-long bend, so entry/exit are 20 m either side

  def _cap(self):
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.corner_speed import corner_cap
    return corner_cap

  def test_alpha_is_exactly_one_at_the_apex(self):
    """THE PROPERTY THAT MAKES OPACITY SAFE TO USE AT ALL: at the apex the
    remaining distance is zero, so the cap IS the corner speed and the ratio is
    exactly 1. No bend can fade to invisible however far away it is — only its
    run-in and run-out fade."""
    v_set, v_corner = 60 / self.MPH, 29 / self.MPH
    gov, cap = _rm.corner_plan_at(0.0, [(0.0, self.HALF, v_corner)], self._cap())
    assert gov == pytest.approx(v_corner)
    assert cap == pytest.approx(v_corner)
    assert _rm.plan_alpha(v_set, gov, cap) == pytest.approx(1.0)

  def test_the_whole_arc_is_solid(self):
    """v3.6.5 — the corner's own extent is FULL opacity end to end, because the
    cap holds the corner speed across it. MUTATION: release from the apex
    (drop half_len) and the exit half of every bend goes translucent while the
    car is still turning."""
    v_set, v_corner = 60 / self.MPH, 29 / self.MPH
    ac = self._cap()
    for m in (-self.HALF, -10.0, 0.0, 10.0, self.HALF):
      gov, cap = _rm.corner_plan_at(m, [(0.0, self.HALF, v_corner)], ac)
      assert _rm.plan_alpha(v_set, gov, cap) == pytest.approx(1.0), m

  def test_it_builds_monotonically_through_the_approach(self):
    """MUTATION: evaluate the envelope at a fixed distance, or drop the
    approach term. The whole point is that it grows as the corner closes."""
    v_set, v_corner = 60 / self.MPH, 29 / self.MPH
    ac = self._cap()
    alphas = []
    for ft in (700, 600, 500, 400, 300, 200, 100, 0):
      gov, cap = _rm.corner_plan_at(-ft * 0.3048, [(0.0, self.HALF, v_corner)], ac)
      alphas.append(_rm.plan_alpha(v_set, gov, cap))
    assert alphas == sorted(alphas)
    assert alphas[-1] == pytest.approx(1.0)
    assert alphas[0] < 0.6

  def test_it_fades_back_to_nothing_after_the_corner(self):
    """The exit is CurveSpeedCap's own release, so the tint ends where the set
    speed genuinely takes over again rather than stopping at the corner."""
    v_set, v_corner = 60 / self.MPH, 29 / self.MPH
    ac = self._cap()
    alphas = []
    # measured from the EXIT, which is HALF beyond the apex
    for m in (0.0, 30.0, 60.0, 90.0, 140.0):
      gov, cap = _rm.corner_plan_at(self.HALF + m, [(0.0, self.HALF, v_corner)], ac)
      alphas.append(_rm.plan_alpha(v_set, gov, cap))
    assert alphas == sorted(alphas, reverse=True)
    assert alphas[0] == pytest.approx(1.0)
    assert alphas[-1] == 0.0
    # THE INTERMEDIATE VALUES ARE THE TEST. An earlier version asserted only
    # the ordering and the ends, and PASSED with the exit fade deleted --
    # [1, 0, 0, 0, 0] is still sorted descending and still ends at zero. The
    # exit has to actually ramp.
    assert 0.0 < alphas[-2] < alphas[1] < 1.0
    assert alphas[1] > 0.3

  def test_the_exit_is_shorter_than_the_entry(self):
    """Not cosmetic: entry uses a budget capped at 1.20 m/s^2 while the release
    is 2.5, so 'brake early, accelerate out' is visible in the shape."""
    v_set, v_corner = 60 / self.MPH, 29 / self.MPH
    ac = self._cap()
    def alpha_at(m):
      gov, cap = _rm.corner_plan_at(m, [(0.0, self.HALF, v_corner)], ac)
      return _rm.plan_alpha(v_set, gov, cap)
    assert alpha_at(self.HALF + 100.0) < alpha_at(-self.HALF - 100.0)

  def test_a_corner_that_does_not_constrain_us_is_invisible(self):
    """0% opacity means 'the set speed decides here'. A bend we would take at
    or above the set speed has nothing to say and must not tint the road."""
    v_set = 30 / self.MPH
    gov, cap = _rm.corner_plan_at(-50.0, [(0.0, self.HALF, 45 / self.MPH)], self._cap())
    assert _rm.plan_alpha(v_set, gov, cap) == 0.0

  def test_no_corners_is_no_plan(self):
    assert _rm.corner_plan_at(0.0, [], self._cap()) == (0.0, 0.0)
    assert _rm.plan_alpha(25.0, 0.0, 0.0) == 0.0

  def test_a_short_entry_is_skipped_not_fatal(self):
    """The corner tuple has grown twice now. A consumer that cannot survive a
    row it does not recognise is how the minimap vanished in v3.6.3."""
    ac = self._cap()
    gov, _c = _rm.corner_plan_at(-40.0, [(0.0, 9.0), (0.0, self.HALF, 12.0)], ac)
    assert gov == pytest.approx(12.0)

  def test_the_slower_of_two_corners_governs(self):
    """The same min() the controller takes, so the ribbon cannot disagree with
    the cap the car is actually holding."""
    ac = self._cap()
    gov, _cap = _rm.corner_plan_at(-80.0, [(0.0, self.HALF, 20.0),
                                           (40.0, self.HALF, 9.0)], ac)
    assert gov == pytest.approx(9.0)

  def test_garbage_never_paints(self):
    for e, g, c in ((float('nan'), 12.0, 14.0), (25.0, float('inf'), 14.0),
                    (25.0, 12.0, float('nan'))):
      assert _rm.plan_alpha(e, g, c) == 0.0

  def test_blend_ends_are_exact(self):
    """alpha 0 is the road, alpha 1 is the corner's colour, and nothing in
    between escapes the two."""
    grey, red = (118, 134, 152), (255, 68, 68)
    assert _rm.blend(grey, red, 0.0) == grey
    assert _rm.blend(grey, red, 1.0) == red
    mid = _rm.blend(grey, red, 0.5)
    assert all(min(grey[i], red[i]) <= mid[i] <= max(grey[i], red[i]) for i in range(3))

  def test_the_ribbon_and_the_cap_are_one_function(self):
    """v3.6.5 — the widget no longer mirrors the controller's release rate, it
    calls the controller's own `corner_cap`. Pinned because "the ribbon cannot
    disagree with the cap" is the whole justification for drawing it."""
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_speed as CS
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import RELEASE_RATE
    assert CS.RELEASE_RATE is RELEASE_RATE
    # and the widget's own value comes from that function, not a local copy
    _gov, cap = _rm.corner_plan_at(45.0, [(0.0, 20.0, 12.0)], self._cap())
    assert cap == pytest.approx(CS.corner_cap(12.0, -45.0, 20.0))
