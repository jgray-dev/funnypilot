"""FunnyPilot v3.7.0 — the follow-distance hologram.

Three things are pinned here and they are different kinds of claim:

  * THE GEOMETRY is pure and is tested as arithmetic: in a curve the segment
    is perpendicular to the path, not to the car; on a hill it takes the road's
    height at that distance; off the end of the path it draws nothing.
  * THE VISIBILITY RULE is tested by driving the real `FollowLine` object:
    shown with longitudinal active and a live published gap — lead or no lead
    since v3.7.1 — and THE TINT is pure arithmetic: white at the line, red
    inside it, and never anything on the far side.
  * THE WIRING is an AST scan, because `augmented_road_view` cannot be imported
    off-device: the draw is behind `safe_draw`, inside the sunnypilot-UI guard,
    and after the model renderer whose transform it borrows.

Drives the real module under the shared pyray stub (one stub, one owner — see
the v3.6.7 process note).
"""
import ast
import pathlib

import numpy as np
import pytest

from openpilot.selfdrive.ui.sunnypilot.onroad.tests.test_hud_imports import _load

_fl = _load('follow_line')
_T = _load('tokens')

_ONROAD = pathlib.Path(__file__).resolve().parents[1]
_ROAD_VIEW = _ONROAD.parents[1] / 'onroad' / 'augmented_road_view.py'
_PLANNER = _ONROAD.parents[2] / 'controls' / 'lib' / 'longitudinal_planner.py'
_PLANNER_SP = _ONROAD.parents[3] / 'sunnypilot' / 'selfdrive' / 'controls' / 'lib' / 'longitudinal_planner.py'


def _straight(n=40, step=2.0, z=0.0):
  x = np.arange(n) * step
  return np.column_stack([x, np.zeros(n), np.full(n, z)])


def _arc(radius=60.0, n=60, step=1.5):
  """A left-hand bend of constant radius, parameterised by arc length."""
  s = np.arange(n) * step
  th = s / radius
  x = radius * np.sin(th)
  y = radius * (1.0 - np.cos(th))
  return np.column_stack([x, y, np.zeros(n)])


class TestTheGeometry:
  def test_straight_road_gives_a_symmetric_gate_at_the_gap(self):
    left, right = _fl.follow_line_segment(_straight(), 30.0)
    assert left[0] == pytest.approx(30.0) and right[0] == pytest.approx(30.0)
    assert left[1] == pytest.approx(_fl.HALF_WIDTH_M)
    assert right[1] == pytest.approx(-_fl.HALF_WIDTH_M)
    assert left[2] == right[2] == 0.0

  def test_in_a_curve_the_gate_follows_the_path_not_the_bonnet(self):
    """THE REQUIREMENT. At 40 m along a 60 m-radius bend the path has swung
    well to the left; a line drawn straight ahead would sit in the oncoming
    lane. The gate's midpoint must be ON the path there."""
    pts = _arc()
    gap = 40.0
    left, right = _fl.follow_line_segment(pts, gap)
    mid = (left + right) / 2.0
    # the path point at x == gap, by interpolation
    y_on_path = float(np.interp(gap, pts[:, 0], pts[:, 1]))
    assert mid[0] == pytest.approx(gap, abs=1e-6)
    assert mid[1] == pytest.approx(y_on_path, abs=1e-3)
    assert abs(y_on_path) > 5.0, "fixture: the bend must actually have swung the path"

  def test_in_a_curve_the_gate_is_perpendicular_to_the_lane(self):
    pts = _arc()
    left, right = _fl.follow_line_segment(pts, 40.0)
    across = (left - right)[:2]
    i = int(np.searchsorted(pts[:, 0], 40.0)) - 1
    along = (pts[i + 1] - pts[i])[:2]
    cos = np.dot(across, along) / (np.linalg.norm(across) * np.linalg.norm(along))
    assert abs(cos) < 1e-6, "the gate must be normal to the path direction"
    # ...and it is still exactly one gate wide, so the normal was unit length.
    assert np.linalg.norm(across) == pytest.approx(2 * _fl.HALF_WIDTH_M)

  def test_on_a_hill_the_gate_takes_the_roads_height_there(self):
    """The model path carries z. At 40 m up a 5% grade the road is 2 m higher
    than under the car, and a line drawn at z=0 would float below the road on
    screen. It must take the interpolated z."""
    pts = _straight()
    pts[:, 2] = pts[:, 0] * 0.05
    left, right = _fl.follow_line_segment(pts, 41.0)
    assert left[2] == pytest.approx(2.05) and right[2] == pytest.approx(2.05)

  def test_interpolates_between_path_points_rather_than_snapping(self):
    pts = _arc()
    a = _fl.follow_line_segment(pts, 40.0)
    b = _fl.follow_line_segment(pts, 40.7)
    # A snapped implementation returns identical points for gaps inside the
    # same path interval; interpolation moves the gate forward.
    assert not np.allclose(a[0], b[0])
    assert b[0][0] > a[0][0]

  def test_beyond_the_path_draws_nothing(self):
    assert _fl.follow_line_segment(_straight(), 500.0) is None

  def test_behind_the_car_draws_nothing(self):
    assert _fl.follow_line_segment(_straight(), 0.0) is None
    assert _fl.follow_line_segment(_straight(), -5.0) is None

  @pytest.mark.parametrize("bad", [None, [], np.zeros((1, 3)), np.zeros((5, 2)), "x", np.full((5, 3), np.nan)])
  def test_unusable_paths_draw_nothing_and_never_raise(self, bad):
    assert _fl.follow_line_segment(bad, 10.0) is None

  def test_non_finite_gap_draws_nothing(self):
    for g in (float('nan'), float('inf'), -float('inf')):
      assert _fl.follow_line_segment(_straight(), g) is None

  def test_a_degenerate_path_with_repeated_points_does_not_divide_by_zero(self):
    pts = _straight()
    pts[10] = pts[11]
    seg = _fl.follow_line_segment(pts, float(pts[10][0]))
    assert seg is not None and np.all(np.isfinite(seg[0])) and np.all(np.isfinite(seg[1]))


class TestTheVisibilityRule:
  """Driven through the REAL FollowLine with the /dev/shm read stubbed."""

  def _line(self, gap, lead):
    fl = _fl.FollowLine()
    fl._read = lambda: (gap, 1.6, lead)
    return fl

  def test_shown_with_long_active_and_a_published_gap_lead_or_not(self):
    """v3.7.1 — A LEAD IS NOT REQUIRED. The bar marks the gap the planner would
    hold; an empty road still has one. MUTATION: put `and bool(lead)` back in
    `target()` and the no-lead case goes dark."""
    assert self._line(35.0, True).target(True)[0] == 1.0
    assert self._line(35.0, False).target(True)[0] == 1.0, "no lead is still a gap to show"
    assert self._line(35.0, True).target(False)[0] == 0.0, "no long control, no gate"
    assert self._line(0.0, True).target(True)[0] == 0.0, "planner not publishing"

  def test_an_absurd_gap_is_not_drawn(self):
    # Past MAX_GAP_M the model path is uncertain and a line there would claim a
    # precision it does not have.
    assert self._line(_fl.MAX_GAP_M + 1.0, True).target(True)[0] == 0.0

  def test_the_gap_holds_its_last_value_while_fading_out(self):
    # While the alpha eases to zero the line must not jump to 0 m first —
    # that would sweep it toward the bonnet as it disappears.
    fl = self._line(35.0, True)
    fl._gap.snap(35.0)
    _, g = fl.target(False)
    assert g == 35.0

  def test_a_short_headway_is_closer_and_still_shown(self):
    # Personality 1 (aggressive) publishes a smaller gap; the line moves in,
    # it does not go away.
    near = self._line(22.0, True).target(True)
    far = self._line(40.0, True).target(True)
    assert near[0] == far[0] == 1.0
    assert near[1] < far[1]

  def test_the_easers_are_the_houses(self):
    # Compared against the tokens module follow_line itself bound (`_fl.T`),
    # not the copy `_load('tokens')` produced: the loader imports each stem
    # into its own module object, so `isinstance` across the two copies is
    # false even for identical classes (test_hud_logic notes the same trap).
    fl = _fl.FollowLine()
    assert isinstance(fl._alpha, _fl.T.Eased) and isinstance(fl._gap, _fl.T.Eased)
    assert fl._alpha.tau == _fl.T.EASE_TAU
    assert fl._gap.tau == _fl.GAP_TAU > _fl.T.EASE_TAU

  def test_draw_survives_a_model_renderer_with_nothing_in_it(self):
    """safe_draw would disable the widget for the session on a raise; this is
    the path that runs on the first frames before the model has published."""
    class _Empty:
      pass
    fl = self._line(35.0, True)
    fl.draw(_Empty(), {}, type("R", (), {"x": 0, "y": 0, "width": 100, "height": 100})())

  def test_draw_projects_through_the_renderers_own_transform(self):
    """The accuracy argument: the gate goes through the SAME matrix the lead
    chevron does. We hand it an identity-ish projective transform and check
    the endpoints land where that transform says."""
    calls = []
    _fl.rl.draw_line_ex = lambda a, b, w, c: calls.append((a, b, w))

    class _Path:
      raw_points = _straight()

    class _MR:
      _path = _Path()
      _path_offset_z = 1.2
      # A projective transform that maps (x, y, z) -> screen (y*10 + 500, z*10 + 300) / 1
      _car_space_transform = np.array([[0, 10, 0], [0, 0, 10], [0, 0, 0]], dtype=float)

    # depth of zero would be rejected; give the transform a constant depth of 1
    _MR._car_space_transform = np.array([[0, 10, 0], [0, 0, 10], [0, 0, 0]], dtype=float)
    _MR._car_space_transform[2] = [0, 0, 0]
    _MR._car_space_transform = np.array([[0, 10, 0], [0, 0, 10], [0.0, 0.0, 0.0]])
    # use x as depth so nothing divides by zero: screen = (10*y/x, 10*z/x)
    _MR._car_space_transform = np.array([[0, 10, 0], [0, 0, 10], [1, 0, 0]], dtype=float)

    fl = self._line(30.0, True)
    fl._alpha.snap(1.0)
    fl._gap.snap(30.0)
    rect = type("R", (), {"x": -1e9, "y": -1e9, "width": 2e9, "height": 2e9})()
    fl.draw(_MR(), {'carControl': type("C", (), {"longActive": True})()}, rect)
    assert calls, "nothing was drawn"
    # v3.7.1 — two strokes (halo + line), no end posts: every call shares the
    # same two endpoints. MUTATION: restore the end-post loop and a call with a
    # different pair of points appears.
    assert len(calls) == len(_fl._STROKES)
    assert all((c[0].x, c[0].y, c[1].x, c[1].y) == (calls[0][0].x, calls[0][0].y, calls[0][1].x, calls[0][1].y)
               for c in calls)
    a, b, _w = calls[0]
    # left endpoint: y=+HALF, z=1.2 at x=30 -> (10*1.45/30, 10*1.2/30)
    assert a.x == pytest.approx(10 * _fl.HALF_WIDTH_M / 30.0)
    assert a.y == pytest.approx(10 * 1.2 / 30.0)
    assert b.x == pytest.approx(-10 * _fl.HALF_WIDTH_M / 30.0)


class TestTheWiring:
  """AST scans — the road view and the planner cannot be imported off-device."""

  def _fn(self, path, name):
    tree = ast.parse(pathlib.Path(path).read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    assert fn is not None, f"{name} not found in {path.name} — re-point this guard"
    return fn

  def test_the_draw_is_behind_safe_draw_and_the_sp_ui_guard(self):
    fn = self._fn(_ROAD_VIEW, "_render")
    src = ast.unparse(fn)
    assert 'safe_draw("follow_line"' in src.replace("'", '"')
    # ...inside an `if gui_app.sunnypilot_ui()` — stock UI must be untouched.
    guarded = False
    for node in ast.walk(fn):
      if isinstance(node, ast.If) and "sunnypilot_ui" in ast.unparse(node.test):
        if "follow_line" in ast.unparse(node.body):
          guarded = True
    assert guarded

  def test_the_draw_comes_after_the_model_renderer(self):
    # It borrows this frame's transform and path, so it has to run after the
    # renderer has updated them.
    src = ast.unparse(self._fn(_ROAD_VIEW, "_render"))
    assert src.index("model_renderer.render(") < src.index("follow_line")

  def test_the_road_view_constructs_it_once(self):
    src = ast.unparse(self._fn(_ROAD_VIEW, "__init__"))
    assert src.count("FollowLine()") == 1

  def test_the_planner_computes_the_gap_from_its_own_constants(self):
    """`get_T_FOLLOW` and `STOP_DISTANCE` live in long_mpc (acados), so the UI
    cannot have them. The planner must be the one doing the arithmetic."""
    src = ast.unparse(self._fn(_PLANNER, "update"))
    assert "_follow_gap_m" in src and "get_T_FOLLOW(" in src and "STOP_DISTANCE" in src
    # And it is the equilibrium gap, not the stopping-inclusive one: no v^2 term.
    line = next(ln for ln in src.splitlines() if "_follow_gap_m =" in ln)
    assert "** 2" not in line and "**2" not in line

  def test_the_planner_publishes_it(self):
    src = ast.unparse(self._fn(_PLANNER_SP, "publish_longitudinal_plan_sp"))
    assert "write_follow_shm(" in src
    assert "leadOne.status" in src

  def test_the_hud_module_reads_shm_lazily(self):
    """The hud/ rule: no IO at import. The channel read is a function-local
    import inside the draw path, never at module scope."""
    tree = ast.parse((_ONROAD / 'hud' / 'follow_line.py').read_text())
    top = {n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert not any("scc_shm" in m for m in top)
    assert "read_follow_shm" in (_ONROAD / 'hud' / 'follow_line.py').read_text()

  def test_the_colour_is_a_token_not_a_literal(self):
    # v3.5.4's rule, applied to the new module: colours come from tokens.py.
    tree = ast.parse((_ONROAD / 'hud' / 'follow_line.py').read_text())
    for node in ast.walk(tree):
      if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("Color"):
        raise AssertionError("follow_line.py must take its colour from tokens.py")
    assert hasattr(_T, "GUIDE") and hasattr(_T, "HOLO")

  def test_the_only_colours_it_can_reach_are_white_and_the_halt_red(self):
    """v3.7.1 — 'it should stay between the white and red colors, no green'.
    Pinned STRUCTURALLY: the module may name no colour token other than GUIDE
    and HALT (HOLO being GUIDE's alias). MUTATION: reference T.ENGAGED anywhere
    in follow_line.py."""
    tree = ast.parse((_ONROAD / 'hud' / 'follow_line.py').read_text())
    colour_tokens = {n for n in dir(_T) if n.isupper() and isinstance(getattr(_T, n), _T.rl.Color)}
    used = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "T" and node.attr in colour_tokens}
    assert used, "anti-vacuous: the scan found no colour tokens at all"
    assert used <= {"GUIDE", "HOLO", "HALT"}, used
    # ...and the neutral end really is neutral: no channel bias, i.e. no hue.
    g = _T.GUIDE
    assert max(g.r, g.g, g.b) - min(g.r, g.g, g.b) <= 6, "GUIDE has a hue"

  def test_there_are_no_end_posts(self):
    """v3.7.1 — 'remove the vertical lines on either end'. One draw call site,
    inside the stroke loop. MUTATION: add a second draw_line_ex."""
    tree = ast.parse((_ONROAD / 'hud' / 'follow_line.py').read_text())
    n = sum(1 for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("draw_line_ex"))
    assert n == 1
    assert "POST_H_M" not in (_ONROAD / 'hud' / 'follow_line.py').read_text()


class TestTheTint:
  """v3.7.1 — a lead INSIDE the line is the planner wanting more gap, and the
  bar says so by turning red. Pure arithmetic, so it is pinned as arithmetic."""

  def test_no_lead_is_white(self):
    assert _fl.tint_for(35.0, False, 10.0) == 0.0

  def test_a_lead_on_or_beyond_the_line_is_white(self):
    assert _fl.tint_for(35.0, True, 35.0) == 0.0
    assert _fl.tint_for(35.0, True, 60.0) == 0.0

  def test_it_is_one_sided(self):
    """NO GREEN. A lead further away than the line must produce exactly the
    same answer as a lead on it, however far away it is: there is no branch on
    that side. MUTATION: return a signed value."""
    for d in (35.1, 50.0, 200.0, 1e6):
      assert _fl.tint_for(35.0, True, d) == 0.0

  def test_it_reddens_as_the_lead_moves_inside_the_line(self):
    ts = [_fl.tint_for(35.0, True, d) for d in (34.0, 30.0, 25.0, 20.0)]
    assert all(0.0 < t <= 1.0 for t in ts)
    assert all(b > a for a, b in zip(ts, ts[1:], strict=False))

  def test_full_red_at_the_fraction_of_the_gap(self):
    g = 40.0
    assert _fl.tint_for(g, True, g * (1.0 - _fl.TINT_FULL_FRAC)) == pytest.approx(1.0)
    assert _fl.tint_for(g, True, 0.0) == 1.0
    assert 0.0 < _fl.TINT_FULL_FRAC < 1.0

  @pytest.mark.parametrize("gap,d", [(float('nan'), 10.0), (35.0, float('nan')), (0.0, 5.0),
                                     (-5.0, 1.0), ("x", 1.0), (35.0, None)])
  def test_garbage_is_white_not_red(self, gap, d):
    assert _fl.tint_for(gap, True, d) == 0.0

  def test_the_draw_blends_toward_halt_when_the_lead_is_inside(self):
    """Through the real draw: the colour handed to raylib moves from GUIDE
    toward HALT with a lead inside the line, and is EXACTLY GUIDE with the lead
    beyond it. MUTATION: drop the radarState read, or lerp the wrong way."""
    calls = []
    _fl.rl.draw_line_ex = lambda a, b, w, c: calls.append(c)

    class _Path:
      raw_points = _straight()

    class _MR:
      _path = _Path()
      _path_offset_z = 1.2
      _car_space_transform = np.array([[0, 10, 0], [0, 0, 10], [1, 0, 0]], dtype=float)

    rect = type("R", (), {"x": -1e9, "y": -1e9, "width": 2e9, "height": 2e9})()

    def frame(d_rel):
      fl = _fl.FollowLine()
      fl._read = lambda: (30.0, 1.6, True)
      fl._alpha.snap(1.0)
      fl._gap.snap(30.0)
      sm = {'carControl': type("C", (), {"longActive": True})(),
            'radarState': type("R", (), {"leadOne": type("L", (), {"status": True, "dRel": d_rel})()})()}
      calls.clear()
      for _ in range(3):
        fl.draw(_MR(), sm, rect)
      return calls[-1]

    beyond = frame(45.0)
    assert (beyond.r, beyond.g, beyond.b) == (_fl.T.GUIDE.r, _fl.T.GUIDE.g, _fl.T.GUIDE.b)
    inside = frame(12.0)
    assert inside.g < _fl.T.GUIDE.g and inside.b < _fl.T.GUIDE.b, "must move toward HALT"
    assert inside.r >= _fl.T.HALT.r - 1 or inside.r >= inside.g, "toward red, not away from it"
