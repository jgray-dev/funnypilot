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
