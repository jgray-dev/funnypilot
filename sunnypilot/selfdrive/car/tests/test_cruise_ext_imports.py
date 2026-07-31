"""FunnyPilot v3.4.2 — import-time regression guard for cruise_ext.

WHY THIS EXISTS: v3.4.0 and v3.4.1 both left the car UNBOOTABLE (stuck on the
comma splash screen, restarts did nothing) because of a single type
annotation:

    def update_speed_limit_assist_v_cruise_non_pcm(self, CS: car.CarState | None = None)
    TypeError: unsupported operand type(s) for |: '_StructModule' and 'NoneType'

`car.CarState` is a capnp _StructModule, not a Python type, so the `|` union
operator raises while the CLASS BODY is being evaluated at import time. That
import sits on manager's startup path (manager -> process_config -> mapd ->
base_map_data -> selfdrive.car.cruise -> cruise_ext), so manager died before
starting anything and the device never got past the splash.

Nothing in the existing suite imported this module, so both versions tested
green and still bricked the car. This test closes that hole: it imports
cruise_ext for real, with only the compiled-extension dependency stubbed.
Plain `x: car.CarState` annotations are fine — only `|` unions on capnp
module objects are not.
"""
import sys
import types

import pytest


def _stub_compiled_deps():
  """Stub only what needs a compiled .so; everything else imports for real."""
  if 'openpilot.common.params' not in sys.modules:
    m = types.ModuleType('openpilot.common.params')

    class _Params:
      def __init__(self, *a, **k):
        pass

      def get(self, *a, **k):
        return None

      def get_bool(self, *a, **k):
        return False

      def put(self, *a, **k):
        pass

    m.Params = _Params
    m.UnknownKeyName = type('UnknownKeyName', (Exception,), {})
    sys.modules['openpilot.common.params'] = m


def test_cruise_ext_imports_cleanly():
  """The import itself is the assertion — a bad annotation raises here."""
  _stub_compiled_deps()
  sys.modules.pop('openpilot.sunnypilot.selfdrive.car.cruise_ext', None)
  try:
    from openpilot.sunnypilot.selfdrive.car.cruise_ext import VCruiseHelperSP
  except ImportError as e:
    pytest.skip(f"unrelated compiled dependency unavailable: {e}")
  assert VCruiseHelperSP is not None


def test_no_capnp_union_annotations_in_cruise_ext():
  """Static guard: capnp module objects don't support `|`, and the failure is
  at import time, so it bypasses every runtime test. Catch it in the source."""
  import pathlib
  import re
  src = (pathlib.Path(__file__).resolve().parents[1] / 'cruise_ext.py').read_text()
  pattern = re.compile(r'(?:\b(?:car|custom|log)\.[A-Za-z_.]+\s*\|\s*None)' +
                       r'|(?:\bNone\s*\|\s*(?:car|custom|log)\.[A-Za-z_.]+)')
  bad = []
  for n, line in enumerate(src.splitlines(), 1):
    code = line.split('#', 1)[0]  # comments may legitimately describe the bug
    if pattern.search(code):
      bad.append(f"line {n}: {line.strip()}")
  assert not bad, f"capnp type in a `|` union — this breaks import and bricks boot: {bad}"
