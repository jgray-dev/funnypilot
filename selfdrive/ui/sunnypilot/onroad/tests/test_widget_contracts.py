"""FunnyPilot v3.6.2 — a Widget subclass must be CONSTRUCTIBLE, and its
public API must still exist for the modules that call it.

WHY THIS TEST EXISTS. The first flash of v3.6.2 put the device in a boot loop:

    TypeError: Can't instantiate abstract class DeveloperUiRenderer
               without an implementation for abstract method '_render'

The v3.6.2 dev-UI rewrite replaced the body of `_render` with two new helper
methods and never re-declared `_render` itself. `system.ui.widgets.Widget` is
an `abc.ABC` whose public `render()` dispatches to an abstract `_render`, so
the class could not be INSTANTIATED at all — and `selfdrive/ui/ui.py`
constructs every layout up front, so the UI process died, manager restarted
it, and it died again. Splash screen, forever, with ssh still working.

EVERY EXISTING GUARD PASSED, and the reason is the whole point of this file:

  * `test_hud_imports.py` imports each module. This is not an import error —
    the module imports fine, the class object is built fine, and the failure
    only happens at `DeveloperUiRenderer()`.
  * it also only scans `hud/`, because that package is deliberately IO-free.
    `developer_ui/` imports `ui_state`, which needs `msgq/ipc_pyx.so`, so it
    CANNOT be imported off-device at all — a runtime check is impossible here.

So this is an AST scan, for the same reason `sunnypilot/tests/
test_capnp_annotations.py` is one: the code path cannot be executed off the
device, and a static check is the only thing that works.

TWO CONTRACTS ARE CHECKED, and the second exists because the same rewrite
deleted TWO more methods that nothing caught:

  1. a Widget subclass defines `_render` somewhere in its in-repo ancestry
  2. an attribute referenced as `SomeClass.thing` from another module is
     actually defined on `SomeClass` — `get_bottom_dev_ui_offset` was deleted
     while `hud_renderer.py` and `driver_state.py` still called it, which
     would have been the NEXT boot loop after `_render` was fixed.
"""
import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[5]
_ONROAD = _ROOT / 'selfdrive' / 'ui' / 'sunnypilot' / 'onroad'
_WIDGET_BASE = _ROOT / 'system' / 'ui' / 'widgets' / '__init__.py'

# Names Widget's own machinery provides to every subclass. Referenced through
# a class rather than an instance only rarely, but allowed when they are.
_BASE_PROVIDED = {
  'render', 'set_rect', 'set_parent_rect', 'set_visible', 'set_enabled',
  'is_visible', 'is_enabled', 'rect', 'parent_rect', 'show_event', 'hide_event',
}


def _py_files():
  return sorted(_ONROAD.rglob('*.py'))


def _is_abstract(fn: ast.FunctionDef) -> bool:
  """Decorated @abstractmethod / @abc.abstractmethod.

  LOAD-BEARING, AND THE FIRST DRAFT OF THIS FILE GOT IT WRONG. `Widget` DOES
  define `_render` — as an abstractmethod — so a checker that treats any
  inherited definition as satisfying the contract concludes every subclass is
  fine and can never fire. An abstract declaration is the OPPOSITE of an
  implementation; it is what creates the requirement.
  """
  for d in fn.decorator_list:
    if isinstance(d, ast.Name) and d.id == 'abstractmethod':
      return True
    if isinstance(d, ast.Attribute) and d.attr == 'abstractmethod':
      return True
  return False


def _classes_in(path: pathlib.Path):
  """{class: (bases, all attribute names, CONCRETE attribute names)}.

  Two name sets, because the two contracts ask different questions: an
  abstract method still EXISTS for an attribute reference, but does not COUNT
  as an implementation.
  """
  try:
    tree = ast.parse(path.read_text(encoding='utf-8'))
  except SyntaxError as e:                                   # pragma: no cover
    pytest.fail(f'{path} does not parse: {e}')
  out = {}
  for node in ast.walk(tree):
    if not isinstance(node, ast.ClassDef):
      continue
    bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
    bases += [b.attr for b in node.bases if isinstance(b, ast.Attribute)]
    names, concrete = set(), set()
    for item in node.body:
      if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.add(item.name)
        if not _is_abstract(item):
          concrete.add(item.name)
      elif isinstance(item, ast.Assign):
        for t in item.targets:
          if isinstance(t, ast.Name):
            names.add(t.id)
            concrete.add(t.id)
      elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
        names.add(item.target.id)
        concrete.add(item.target.id)
    out[node.name] = (bases, names, concrete)
  return out


def _all_classes():
  """Every class in the onroad tree, plus the Widget base itself so ancestry
  and inherited names resolve."""
  merged = {}
  for p in [*_py_files(), _WIDGET_BASE]:
    for name, spec in _classes_in(p).items():
      merged.setdefault(name, spec)
  return merged


def _onroad_class_names():
  """Only classes DEFINED in the onroad tree are policed.

  The Widget base file is parsed for ancestry and inherited attributes, but
  its own classes are not checked: `NavWidget` is an intentional intermediate
  abstract base that `SettingsLayout` and friends complete, and flagging it
  would be a false positive. This test is about the tree it lives in.
  """
  names = set()
  for p in _py_files():
    names |= set(_classes_in(p))
  return names


def _defines(classes, name, attr, _seen=None, concrete_only=False):
  """Is `attr` present on `name` or an in-repo ancestor?

  `concrete_only` skips abstract declarations, which is what distinguishes
  "this attribute can be referenced" from "this class can be instantiated".
  """
  _seen = _seen or set()
  if name in _seen or name not in classes:
    return False
  _seen.add(name)
  bases, names, concrete = classes[name]
  if attr in (concrete if concrete_only else names):
    return True
  return any(_defines(classes, b, attr, _seen, concrete_only) for b in bases)


def _implements(classes, name, attr):
  return _defines(classes, name, attr, concrete_only=True)


def _is_widget(classes, name, _seen=None):
  _seen = _seen or set()
  if name in _seen or name not in classes:
    return False
  _seen.add(name)
  bases = classes[name][0]
  return 'Widget' in bases or any(_is_widget(classes, b, _seen) for b in bases)


class TestTheScanIsNotVacuous:
  """Both checks walk a discovered file list. If discovery breaks they would
  pass by finding nothing, which is the failure mode this file is about."""

  def test_there_are_files_to_scan(self):
    assert len(_py_files()) > 5

  def test_the_widget_base_was_found_and_is_abstract(self):
    classes = _all_classes()
    assert 'Widget' in classes, 'the Widget base did not parse; ancestry is broken'
    assert '_render' in classes['Widget'][1], 'Widget must declare _render'
    assert '_render' not in classes['Widget'][2], (
      'Widget._render must be ABSTRACT; a concrete body silently voids this guard')

  def test_it_finds_the_class_that_broke(self):
    classes = _all_classes()
    assert 'DeveloperUiRenderer' in classes
    assert _is_widget(classes, 'DeveloperUiRenderer')


class TestEveryWidgetIsConstructible:
  """THE REGRESSION GUARD. `Widget._render` is abstract, so a subclass that
  does not define it (or inherit a concrete one) raises TypeError the moment
  anything constructs it — and `selfdrive/ui/ui.py` constructs every layout at
  startup, which makes that a boot loop rather than a broken widget."""

  def test_all_widget_subclasses_implement_render(self):
    classes = _all_classes()
    onroad = _onroad_class_names()
    widgets = [n for n in onroad if _is_widget(classes, n)]
    assert widgets, 'no Widget subclasses found — the scan is broken'
    missing = [n for n in widgets if not _implements(classes, n, '_render')]
    assert not missing, (
      'these Widget subclasses cannot be instantiated (abstract _render): '
      + ', '.join(sorted(missing))
      + ' — this is the v3.6.2 boot loop; see the module docstring')

  def test_the_check_would_catch_a_missing_render(self):
    """Anti-vacuous: a synthetic subclass without _render must be flagged, or
    a bug in `_defines` would make the whole file silently pass."""
    fake = {'Widget': ([], {'_render'}, set()),          # abstract: declared, not concrete
            'Good': (['Widget'], {'_render'}, {'_render'}),
            'Bad': (['Widget'], {'_draw_everything'}, {'_draw_everything'}),
            'GrandchildOfGood': (['Good'], set(), set())}
    assert _implements(fake, 'Good', '_render')
    assert _implements(fake, 'GrandchildOfGood', '_render')
    assert not _implements(fake, 'Bad', '_render')
    # ...and the abstract declaration alone must NOT satisfy it
    assert _defines(fake, 'Bad', '_render')               # it is inherited...
    assert not _implements(fake, 'Bad', '_render')        # ...but not implemented


class TestCrossModuleAttributesExist:
  """THE SECOND DEFECT THE SAME REWRITE INTRODUCED, and nothing caught it
  either: `get_bottom_dev_ui_offset` was deleted while `hud_renderer.py` and
  `driver_state.py` still called it through the class. That is an
  AttributeError at construction time — i.e. the identical boot loop, one
  round trip later.

  Only `ClassName.attr` is checked, never `instance.attr`: the receiver has to
  be a bare Name that resolves to a class we parsed, so there is nothing to
  guess about and no false positives from duck typing.
  """

  def _references(self):
    classes = _all_classes()
    hits = []
    for p in _py_files():
      tree = ast.parse(p.read_text(encoding='utf-8'))
      for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in classes):
          hits.append((p, node.lineno, node.value.id, node.attr))
    return classes, hits

  def test_there_are_references_to_check(self):
    _classes, hits = self._references()
    assert len(hits) > 3

  def test_every_referenced_class_attribute_is_defined(self):
    classes, hits = self._references()
    bad = [f'{p.relative_to(_ROOT)}:{ln} -> {cls}.{attr}'
           for p, ln, cls, attr in hits
           if attr not in _BASE_PROVIDED and not _defines(classes, cls, attr)]
    assert not bad, (
      'these reference class attributes that do not exist:\n  '
      + '\n  '.join(sorted(bad)))

  def test_the_check_would_catch_the_deleted_helper(self):
    """Anti-vacuous, and named after the real one: the scan must actually
    reject an attribute that is not on the class."""
    fake = {'Widget': ([], set(), set()),
            'DeveloperUiRenderer': (['Widget'], {'BOTTOM_BAR_HEIGHT'}, {'BOTTOM_BAR_HEIGHT'})}
    assert _defines(fake, 'DeveloperUiRenderer', 'BOTTOM_BAR_HEIGHT')
    assert not _defines(fake, 'DeveloperUiRenderer', 'get_bottom_dev_ui_offset')


# ── v3.6.6: two HUD behaviours that cannot be exercised off-device ──────────
#
# `hud_renderer.py` imports ui_state, which needs msgq/ipc_pyx.so, so neither of
# these can be tested by running them. Same reason the widget contracts above
# are an AST scan: where the path cannot execute off the device, a static check
# is the only check there is.

def _hud_renderer_src():
  import pathlib
  p = pathlib.Path(__file__).resolve().parents[1] / "hud_renderer.py"
  return p.read_text()


def _fn_node(src: str, name: str):
  import ast
  for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name == name:
      return node
  raise AssertionError(f"{name} not found — this scan would pass vacuously")


def _fn_src(src: str, name: str) -> str:
  """The function's CODE, with its docstring removed.

  THE DOCSTRING HAS TO GO, and finding that out was the point. The first cut of
  the minimap guard asserted `"map.enabled" not in body` and failed on its own
  explanation of why the gate was removed. That is the v3.5.8 lesson inverted:
  a test a comment can satisfy is not a test, and neither is one a comment can
  break."""
  import ast
  node = _fn_node(src, name)
  body = [n for n in node.body
          if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                  and isinstance(n.value.value, str))]
  return "\n".join(ast.get_source_segment(src, n) or "" for n in body)


class TestTheMinimapIsAlwaysDrawn:
  """v3.6.6 — it used to be gated on `smartCruiseControl.map.enabled`, which is
  SCC-M v2's `is_enabled` = `long_enabled and toggle`. `long_enabled` is
  `carControl.enabled`, so with only LATERAL engaged the whole minimap vanished
  — exactly when a driver is most interested in the road ahead.

  THE DATA WAS THERE THE WHOLE TIME: `_refresh_corners` deliberately runs before
  the `is_enabled` check in `update()`, and `write_corners_shm` publishes every
  frame. The gate hid a live widget and saved no work.
  """

  def test_the_engagement_gate_is_gone(self):
    """MUTATION: put the `if not ...map.enabled: return` back.

    STRUCTURAL, NOT TEXTUAL — see _fn_src. Any bare `return` inside the function
    is a gate on drawing, whatever it is spelled as."""
    import ast
    node = _fn_node(_hud_renderer_src(), "_draw_route_map")
    for n in ast.walk(node):
      assert not (isinstance(n, ast.Return) and n.value is None), \
          "no early return may gate the minimap"
      assert not (isinstance(n, ast.Attribute) and n.attr == "enabled"), \
          "the minimap must not consult an enabled flag"

  def test_it_is_still_actually_drawn(self):
    """Anti-vacuous: deleting the whole function would satisfy the above."""
    assert "self._route_map.render(" in _fn_src(_hud_renderer_src(), "_draw_route_map")


class TestTheCurveWarningIsAGlowNotABanner:
  """v3.6.6 — "instead of a banner alert saying sharp corner ahead, please
  remove it and rather pulse the state glow around the edges of the screen with
  orange as we approach the corner."

  A banner is READ; an edge pulse is FELT, and the peripheral edge is already
  this HUD's engagement-state channel — so the warning arrives in the driver's
  vision without asking for a glance at text.
  """

  def test_the_banner_raise_site_is_gone(self):
    """MUTATION: re-add `self.events.add(EventName.speedTooHigh)` to
    selfdrived. The alert itself still exists for its stock purpose
    (car_specific.py above MAX_CTRL_SPEED); what must not come back is SCC-M v2
    raising it."""
    import pathlib
    sd = (pathlib.Path(__file__).resolve().parents[4]
          / "selfdrived/selfdrived.py").read_text()
    import ast
    for n in ast.walk(ast.parse(sd)):
      assert not (isinstance(n, ast.Attribute) and n.attr == "speedTooHigh"), \
          "SCC-M v2 must not raise the banner any more"
      assert not (isinstance(n, ast.Name)
                  and "corner_warning" in n.id), "no shm poll in selfdrived"

  def test_the_glow_carries_it_instead(self):
    """Both halves: the COLOUR changes and the PULSE changes, so the warning is
    distinguishable from the override breath by rhythm as well as hue."""
    src = _hud_renderer_src()
    assert "_corner_warn" in _fn_src(src, "state_color")
    assert "_corner_warn" in _fn_src(src, "glow_intensity")

  def test_it_is_polled_not_read_every_frame(self):
    """The value changes on the 20 Hz planner and the pulse lasts seconds, so a
    per-frame read would be free of information and cost a syscall on the
    render path."""
    body = _fn_src(_hud_renderer_src(), "_update_derived")
    assert "read_corner_warning_shm" in body
    assert "self._warn_t" in body, "the poll must be throttled"
