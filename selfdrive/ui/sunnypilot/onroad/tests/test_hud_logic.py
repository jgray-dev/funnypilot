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

from openpilot.selfdrive.ui.sunnypilot.onroad.tests.test_hud_imports import _load

_rm = _load('route_map')
_ss = _load('speed_sign')
_tok = _load('tokens')

ramp_color = _rm.ramp_color
to_ego_frame = _rm.to_ego_frame
halo_spec = _ss.halo_spec


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
