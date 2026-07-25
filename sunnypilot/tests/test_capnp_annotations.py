"""FunnyPilot v3.4.3 — repo-wide guard against capnp types in `|` unions.

WHY THIS EXISTS
---------------
v3.4.0 and v3.4.1 both shipped with a full green test suite and both left the
car UNBOOTABLE — stuck on the comma splash, unrecoverable by restarting —
because of one annotation:

    def update_speed_limit_assist_v_cruise_non_pcm(self, CS: car.CarState | None = None)
    TypeError: unsupported operand type(s) for |: '_StructModule' and 'NoneType'

`car.CarState` is a capnp `_StructModule`, i.e. a module-level OBJECT, not a
Python type. It does not implement `__or__`, so `X | None` raises. The raise
happens while the module is being IMPORTED, so no runtime test can ever reach
it — and cruise_ext sits on manager's startup path (manager -> process_config
-> mapd_manager -> osm_map_data -> base_map_data -> selfdrive.car.cruise ->
cruise_ext), which is why manager died before starting a single process.

v3.4.2 added a guard, but only over cruise_ext.py. This one covers the whole
repo, because the next occurrence will be in a different file.

WHAT COUNTS AS DANGEROUS — this is about EVALUATION CONTEXT, not text
--------------------------------------------------------------------
Python evaluates some annotations eagerly and merely records others. Verified
empirically (see CLAUDE.md v3.4.3 notes):

  RAISES  def f(x: car.CarState | None)      parameter annotations
  RAISES  def f() -> car.CarState | None     return annotations
  RAISES  class C: CP: car.CarParams | None  class-BODY annotated assignment
  SAFE    self.CP: car.CarParams | None = None   inside a method body
  SAFE    x: car.CarState | None = None          local, inside a function body

That last pair is why this check is an AST walk and not a grep: `ui_state.py`
legitimately uses the safe form, and a text-matching guard would demand a
pointless "fix" there while a class-body union elsewhere would still brick the
car. Only annotations in eagerly-evaluated positions are reported.

THE FIX when this test fires: drop the union. `CS=None` (unannotated) or a
plain `CS: car.CarState` both work. `from __future__ import annotations` would
also defer evaluation, but is deliberately NOT the recommended fix here — it
changes behavior for the whole module and this codebase does not use it.
"""
import ast
import pathlib

import pytest

# capnp schema modules whose attributes are _StructModule objects, not types
CAPNP_ROOTS = {'car', 'custom', 'log', 'legacy'}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# vendored / generated / not-ours trees
SKIP_PARTS = {
  '.git', 'node_modules', 'third_party', 'teleoprtc', 'teleoprtc_repo',
  'body', 'c_generated_code', '__pycache__', '.venv', 'venv', 'site-packages',
}


def _root_name(node: ast.AST) -> str | None:
  """'car.CarState.ButtonEvent' -> 'car'; anything else -> None."""
  while isinstance(node, ast.Attribute):
    node = node.value
  return node.id if isinstance(node, ast.Name) else None


def _capnp_union_names(annotation: ast.AST | None) -> list[str]:
  """Names of capnp attributes used as a DIRECT operand of a `|` union.

  Only a bare `car.CarState | None` raises. `list[custom.X] | None` is fine —
  `list[anything]` builds a types.GenericAlias, which DOES implement __or__,
  so the capnp object never has `|` applied to it. Verified empirically;
  sunnypilot/models/fetcher.py relies on exactly that and is correct.
  Subscripted forms (`Optional[car.X]`, `list[car.X]`) are likewise safe.
  """
  if annotation is None:
    return []
  found = []
  for node in ast.walk(annotation):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
      for side in (node.left, node.right):
        # a nested BinOp (`A | B | None`) is reached by ast.walk in its own right
        if isinstance(side, ast.Attribute) and _root_name(side) in CAPNP_ROOTS:
          found.append(ast.unparse(side))
  return found


def _eager_annotations(tree: ast.Module):
  """Yield (lineno, annotation) for annotations Python evaluates at import.

  Function signatures always evaluate (params + return), wherever they live.
  Annotated assignments evaluate ONLY at module or class level — inside a
  function body they are not evaluated at all.
  """
  def walk(node: ast.AST, in_function: bool):
    for child in ast.iter_child_nodes(node):
      if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = child.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
        for p in params:
          if p is not None and p.annotation is not None:
            yield p.lineno, p.annotation
        if child.returns is not None:
          yield child.lineno, child.returns
        yield from walk(child, True)
      elif isinstance(child, ast.ClassDef):
        # a class body executes immediately, even nested inside a function
        yield from walk(child, False)
      elif isinstance(child, ast.AnnAssign):
        if not in_function and child.annotation is not None:
          yield child.lineno, child.annotation
        yield from walk(child, in_function)
      else:
        yield from walk(child, in_function)

  yield from walk(tree, False)


def _python_files():
  for path in REPO_ROOT.rglob('*.py'):
    if SKIP_PARTS.isdisjoint(path.parts):
      yield path


def _offenders_in(path: pathlib.Path) -> list[str]:
  try:
    tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
  except (SyntaxError, ValueError, OSError):
    return []  # not our concern here; a broken file fails elsewhere
  out = []
  for lineno, annotation in _eager_annotations(tree):
    for name in _capnp_union_names(annotation):
      rel = path.relative_to(REPO_ROOT)
      out.append(f"{rel}:{lineno}: `{name}` in a `|` union")
  return out


def test_no_capnp_unions_in_evaluated_annotations():
  """A capnp type in an eagerly-evaluated annotation raises at IMPORT time,
  so it bypasses every runtime test and can brick the boot. See module docstring."""
  offenders = sorted(o for p in _python_files() for o in _offenders_in(p))
  assert not offenders, (
    "capnp module objects do not support `|`; these annotations raise TypeError at "
    "import time and will prevent the device from booting:\n  " + "\n  ".join(offenders)
    + "\n\nFix: drop the union (`CS=None` or plain `CS: car.CarState`)."
  )


def test_scan_actually_covers_the_repo():
  """Guard the guard: if the file walk silently matched nothing, the test above
  would pass vacuously forever."""
  files = list(_python_files())
  assert len(files) > 500, f"expected to scan the repo, only found {len(files)} files"
  names = {f.name for f in files}
  assert 'cruise_ext.py' in names, "the file that caused the v3.4.0 brick is not being scanned"


@pytest.mark.parametrize('source,expected', [
  # dangerous — evaluated at import
  ('def f(x: car.CarState | None): pass', True),
  ('def f() -> car.CarState | None: pass', True),
  ('class C:\n  CP: car.CarParams | None = None', True),
  ('def f(*, x: custom.CarParamsSP | None = None): pass', True),
  ('def f(**kw: log.Event | None): pass', True),
  ('class C:\n  def m(self):\n    class D:\n      x: car.CarState | None = None', True),
  # safe — not evaluated, or not capnp
  ('class C:\n  def __init__(self):\n    self.CP: car.CarParams | None = None', False),
  ('def f():\n  x: car.CarState | None = None', False),
  ('def f(x: car.CarState): pass', False),
  ('def f(x: int | None): pass', False),
  ('def f(x: "car.CarState | None"): pass', False),  # string annotation, not evaluated
  # subscripted: list[...] is a GenericAlias and DOES support `|` (fetcher.py)
  ('def f() -> list[custom.ModelManagerSP.ModelBundle] | None: pass', False),
  ('def f(x: dict[str, car.CarState] | None = None): pass', False),
  # ...but a bare capnp operand alongside a subscripted one is still fatal
  ('def f(x: list[int] | car.CarState | None = None): pass', True),
])
def test_detector_semantics(source, expected, tmp_path):
  """The detector must match Python's real evaluation rules — including NOT
  flagging the `self.CP: car.CarParams | None` form that ui_state.py relies on."""
  p = tmp_path / 'sample.py'
  p.write_text(source)
  tree = ast.parse(source)
  hits = [n for _, ann in _eager_annotations(tree) for n in _capnp_union_names(ann)]
  assert bool(hits) is expected, f"{source!r} -> {hits}"
