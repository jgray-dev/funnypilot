"""FunnyPilot v3.5.0 — the onroad HUD package must always be importable.

WHY THIS IS THE MOST IMPORTANT TEST IN THE PACKAGE. `selfdrive/ui/ui.py` draws
BOTH the offroad and the onroad screens from one process, and `MainLayout`
constructs all three layouts up front. So an import-time failure anywhere in
the onroad tree does not cost you the onroad screen — it costs you the WHOLE
UI, including settings, which is where you would flash your way out of a bad
build. That is the difference between "a widget looks wrong" and "the device
needs a cable".

Two guards, deliberately different in kind:

  1. A real import of every module in hud/, with only the compiled/graphics
     dependencies stubbed. This catches syntax errors, bad relative imports,
     module-level work that needs a GPU, and anything that touches the
     filesystem while the module body runs.
  2. An AST scan for capnp types used as a direct operand of `|`. This is the
     exact defect that made v3.4.0 and v3.4.1 unbootable: `car.CarState | None`
     in a parameter annotation raises TypeError while the `def` executes, i.e.
     at import. The repo-wide version lives in
     sunnypilot/tests/test_capnp_annotations.py; this is a local, fast copy so
     the failure names this package.

Also pinned here: nothing in hud/ may read the filesystem or /dev/shm at import
or construction time. The route map's Params handle in particular MUST stay
lazy, because /dev/shm/params does not exist offroad.
"""
import ast
import importlib.util
import pathlib
import sys
import types

import pytest

_HUD = pathlib.Path(__file__).resolve().parents[1] / 'hud'
_MODULES = sorted(p.stem for p in _HUD.glob('*.py') if p.stem != '__init__')


def _stub_graphics():
  """Stub only what genuinely cannot exist off-device: the raylib binding and
  the app singleton that owns the GL context and the fonts."""
  if 'pyray' not in sys.modules:
    rl = types.ModuleType('pyray')

    class _C:
      def __init__(self, r=0, g=0, b=0, a=255):
        self.r, self.g, self.b, self.a = r, g, b, a

    class _R:
      def __init__(self, x=0, y=0, width=0, height=0):
        self.x, self.y, self.width, self.height = x, y, width, height

    class _V:
      def __init__(self, x=0, y=0):
        self.x, self.y = x, y

    rl.Color, rl.Rectangle, rl.Vector2 = _C, _R, _V
    rl.Font = object
    rl.WHITE = _C(255, 255, 255, 255)
    rl.BLACK = _C(0, 0, 0, 255)
    for fn in ('draw_text_ex', 'draw_rectangle', 'draw_rectangle_rec', 'draw_rectangle_rounded',
               'draw_rectangle_rounded_lines_ex', 'draw_rectangle_lines_ex', 'draw_circle',
               'draw_ring', 'draw_line_ex', 'draw_triangle_fan', 'draw_rectangle_gradient_v',
               'draw_rectangle_gradient_h', 'begin_scissor_mode', 'end_scissor_mode',
               'measure_text_ex', 'color_alpha'):
      setattr(rl, fn, lambda *a, **k: None)
    sys.modules['pyray'] = rl

  for name, attrs in (
    ('openpilot.system.ui.lib.application', {
      'gui_app': types.SimpleNamespace(font=lambda *a: None, target_fps=60,
                                       texture=lambda *a, **k: None,
                                       sunnypilot_ui=lambda: True),
      'FontWeight': types.SimpleNamespace(BOLD='b', SEMI_BOLD='sb', MEDIUM='m', NORMAL='n'),
      'FONT_SCALE': 1.0,
      'font_fallback': lambda f: f,
    }),
    ('openpilot.system.ui.lib.text_measure', {
      'measure_text_cached': lambda font, text, size, spacing=0: types.SimpleNamespace(
        x=len(text) * size * 0.5, y=size),
    }),
    # swaglog pulls in zmq, which is a compiled dep this container lacks. It is
    # present on device; stubbing it keeps the test about OUR modules.
    ('openpilot.common.swaglog', {
      'cloudlog': types.SimpleNamespace(exception=lambda *a, **k: None,
                                        error=lambda *a, **k: None,
                                        warning=lambda *a, **k: None,
                                        info=lambda *a, **k: None),
    }),
  ):
    if name not in sys.modules:
      m = types.ModuleType(name)
      for k, v in attrs.items():
        setattr(m, k, v)
      sys.modules[name] = m


def _load(stem: str):
  _stub_graphics()
  spec = importlib.util.spec_from_file_location(
    f'openpilot.selfdrive.ui.sunnypilot.onroad.hud.{stem}', _HUD / f'{stem}.py')
  mod = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = mod
  spec.loader.exec_module(mod)
  return mod


class TestEveryModuleImports:
  def test_there_are_modules_to_check(self):
    """Anti-vacuous: a glob that silently matched nothing would make every
    other test in this class pass without importing a thing."""
    assert len(_MODULES) >= 4, _MODULES

  @pytest.mark.parametrize("stem", _MODULES)
  def test_imports_clean(self, stem):
    """MUTATION: any syntax error, bad import path, or module-level work that
    needs a screen. This is the guard between a bad widget and a dead device."""
    assert _load(stem) is not None


class TestNoCapnpUnions:
  """MUTATION: write `car.CarState | None` (or custom./log.) in an annotation.

  Parameter annotations, return annotations and class-body annotated
  assignments are all evaluated as the `def`/class body executes, i.e. at
  import. capnp module objects do not implement `__or__`, so it raises there.
  This is what bricked v3.4.0 and v3.4.1.
  """

  @pytest.mark.parametrize("stem", _MODULES)
  def test_no_capnp_in_union(self, stem):
    tree = ast.parse((_HUD / f'{stem}.py').read_text())
    bad = []

    def flag(node, where):
      if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
          n = side
          while isinstance(n, ast.Attribute):
            n = n.value
          if isinstance(n, ast.Name) and n.id in ('car', 'custom', 'log', 'legacy'):
            bad.append(f"{stem}: {where}")

    for node in ast.walk(tree):
      if isinstance(node, ast.FunctionDef):
        for a in list(node.args.args) + list(node.args.kwonlyargs):
          if a.annotation is not None:
            flag(a.annotation, f"param {a.arg} of {node.name}()")
        if node.returns is not None:
          flag(node.returns, f"return of {node.name}()")
      elif isinstance(node, ast.ClassDef):
        for sub in node.body:
          if isinstance(sub, ast.AnnAssign) and sub.annotation is not None:
            flag(sub.annotation, f"class body of {node.name}")

    assert not bad, bad

  def test_detector_actually_detects(self):
    """Anti-vacuous self-check: the scan above must fail on the real defect."""
    tree = ast.parse("def f(CS: car.CarState | None = None): pass")
    found = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)]
    assert found


class TestNothingTouchesTheFilesystemAtImport:
  """MUTATION: create the /dev/shm Params handle in RouteMap.__init__ or at
  module scope. Offroad that path does not exist, and this package is imported
  by the process that draws the offroad screen."""

  def test_route_map_params_handle_is_lazy(self):
    mod = _load('route_map')
    rm = mod.RouteMap()
    assert rm._params is None
    assert rm._tried_params is False

  @staticmethod
  def _eagerly_evaluated(tree):
    """Nodes that actually run when the module is imported.

    Class bodies DO execute at import and are therefore included; function and
    lambda bodies do not and are pruned. Walking without that distinction is
    the difference between a useful guard and one that flags every correctly
    lazy accessor — which is exactly what the first draft of this test did to
    RouteMap._mem().
    """
    out = []
    stack = list(tree.body)
    while stack:
      node = stack.pop()
      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # decorators and default values DO run at import; the body does not
        stack.extend(node.decorator_list)
        stack.extend(d for d in node.args.defaults if d is not None)
        continue
      out.append(node)
      for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Lambda):
          continue
        stack.append(child)
    return out

  def test_no_module_level_io(self):
    for stem in _MODULES:
      tree = ast.parse((_HUD / f'{stem}.py').read_text())
      for sub in self._eagerly_evaluated(tree):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
          assert sub.func.id not in ('open', 'Params'), f"{stem}: {sub.func.id}() at import"

  def test_the_io_detector_is_not_vacuous(self):
    """A guard that cannot fire is not a guard."""
    tree = ast.parse("from openpilot.common.params import Params\n_p = Params('/dev/shm/params')\n")
    hits = [n for n in self._eagerly_evaluated(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'Params']
    assert hits, "the module-level IO detector stopped detecting"
