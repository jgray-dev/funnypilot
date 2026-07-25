"""FunnyPilot v3.4.0 — longitudinal status dot classification.

Only the pure classify() function is tested: the renderer itself imports
pyray (the on-device raylib binding), which is unavailable off-device — the
same reason no other onroad renderer has test coverage. classify() is
deliberately factored out so the actual decision logic IS testable.

The contract: the dot is a function of the COMMANDED accel (the value packed
into the car's SCC frame) and explicit control-state booleans. No measured
acceleration, no pitch, no coast estimate — v3.3.9 used a pitch-derived coast
line and that is precisely what these tests exist to prevent regressing to.
"""
import importlib.util
import pathlib
import sys
import types

_SRC = pathlib.Path(__file__).resolve().parents[1] / 'long_status_dot.py'


def _load_classify():
  """Load classify() without importing pyray (stubbed) or the ui_state chain."""
  for name in ('pyray', 'openpilot.selfdrive.ui.ui_state', 'openpilot.system.ui.widgets'):
    if name not in sys.modules:
      stub = types.ModuleType(name)
      if name == 'pyray':
        stub.Color = lambda *a: a
        stub.Rectangle = object
      elif name.endswith('ui_state'):
        stub.ui_state = None
      else:
        stub.Widget = type('Widget', (), {'__init__': lambda self: None})
      sys.modules[name] = stub
  spec = importlib.util.spec_from_file_location('_lsd', _SRC)
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


_m = _load_classify()
classify = _m.classify


class TestClassify:
  def test_inactive_is_gray_regardless_of_command(self):
    for accel in (-3.0, -0.5, 0.0, 0.5, 2.0):
      assert classify(False, accel, False) == 'gray'

  def test_positive_command_is_green(self):
    assert classify(True, 0.5, False) == 'green'
    assert classify(True, 2.0, False) == 'green'

  def test_light_positive_command_is_still_green(self):
    # "green should indicate gas being applied in any extent"
    assert classify(True, 0.05, False) == 'green'

  def test_negative_command_is_red(self):
    assert classify(True, -0.5, False) == 'red'
    assert classify(True, -3.0, False) == 'red'

  def test_light_negative_command_is_still_red(self):
    # "any braking controls = red dot", at any rate
    assert classify(True, -0.05, False) == 'red'

  def test_zero_command_is_gray(self):
    assert classify(True, 0.0, False) == 'gray'

  def test_gas_gating_is_gray(self):
    # a gate holding the throttle off is neither gas nor brakes
    assert classify(True, -0.3, True) == 'gray'
    assert classify(True, 0.0, True) == 'gray'

  def test_firm_braking_overrides_gas_gating_flag(self):
    # a real brake application during an approach must not be masked gray
    assert classify(True, -1.5, True) == 'red'

  def test_no_pitch_or_measured_accel_inputs(self):
    # regression guard for the v3.3.9 mistake: classify takes exactly the
    # commanded accel and explicit flags — nothing derived from the IMU
    import inspect
    params = list(inspect.signature(classify).parameters)
    assert params == ['long_active', 'accel', 'gas_gating']
