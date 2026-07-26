"""FunnyPilot v3.4.4 — longitudinal status dot classification.

Only the pure classify() function is tested: the renderer itself imports
pyray (the on-device raylib binding), which is unavailable off-device — the
same reason no other onroad renderer has test coverage. classify() is
deliberately factored out so the actual decision logic IS testable.

The contract: red comes from the CAR'S OWN brake-lamp bit (TCS13.BrakeLight,
relayed over /dev/shm by card), green from the COMMANDED accel, gray from
explicit control-state booleans. No measured acceleration, no pitch, no coast
estimate — v3.3.9 used a pitch-derived coast line and that is precisely what
these tests exist to prevent regressing to.

v3.4.4 changed what "red" means: it used to be any commanded deceleration,
which lit red while the car was merely on reduced throttle. TestReducedThrottle
below is the regression guard for that specific complaint.
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

# convenience: brake lamp off, which is the normal case for most of these
OFF = False
ON = True
UNKNOWN = None


class TestClassify:
  def test_inactive_is_gray_regardless_of_everything(self):
    for accel in (-3.0, -0.5, 0.0, 0.5, 2.0):
      for lamp in (OFF, ON, UNKNOWN):
        assert classify(False, accel, False, lamp) == 'gray'

  def test_positive_command_is_green(self):
    assert classify(True, 0.5, False, OFF) == 'green'
    assert classify(True, 2.0, False, OFF) == 'green'

  def test_light_positive_command_is_still_green(self):
    # "green should indicate gas being applied in any extent"
    assert classify(True, 0.05, False, OFF) == 'green'

  def test_brake_lamp_is_red(self):
    # "red if we're applying any braking force (brake lights on)"
    assert classify(True, -3.0, False, ON) == 'red'
    assert classify(True, -0.05, False, ON) == 'red'

  def test_brake_lamp_is_red_even_with_a_positive_command(self):
    # the lamp is ground truth about the actuator; a stale/optimistic command
    # must not talk us out of it
    assert classify(True, 0.5, False, ON) == 'red'

  def test_zero_command_is_gray(self):
    assert classify(True, 0.0, False, OFF) == 'gray'

  def test_gas_gating_is_gray(self):
    # a gate holding the throttle off is neither gas nor brakes
    assert classify(True, -0.3, True, OFF) == 'gray'
    assert classify(True, 0.0, True, OFF) == 'gray'

  def test_brake_lamp_overrides_gas_gating_flag(self):
    # a real brake application during an approach must not be masked gray
    assert classify(True, -1.5, True, ON) == 'red'


class TestReducedThrottle:
  """The v3.4.4 bug report, verbatim: 'the dot goes red even if we're still
  using the throttle but at a lower amount.'"""

  def test_negative_command_without_brake_lamp_is_not_red(self):
    for accel in (-0.05, -0.3, -0.9, -2.0):
      assert classify(True, accel, False, OFF) != 'red'

  def test_negative_command_without_brake_lamp_is_gray(self):
    assert classify(True, -0.9, False, OFF) == 'gray'

  def test_firm_command_still_needs_the_lamp(self):
    # -1.5 m/s^2 used to be unconditionally red via the old _BRAKE_FIRM
    # constant. It is now the car's call, not a threshold's.
    assert classify(True, -1.5, False, OFF) == 'gray'
    assert classify(True, -1.5, False, ON) == 'red'


class TestUnknownLamp:
  """None must be handled explicitly — never silently treated as 'brakes off'."""

  def test_unknown_falls_back_to_commanded_decel(self):
    assert classify(True, -0.5, False, UNKNOWN) == 'red'
    assert classify(True, -0.05, False, UNKNOWN) == 'red'

  def test_unknown_still_greens_on_throttle(self):
    assert classify(True, 0.5, False, UNKNOWN) == 'green'

  def test_unknown_still_grays_on_gating(self):
    assert classify(True, -0.5, True, UNKNOWN) == 'gray'

  def test_unknown_is_not_false(self):
    # the fallback must actually differ from a known-off lamp, otherwise the
    # None branch is dead code and a missing publisher would look like
    # "brakes are definitely off"
    assert classify(True, -0.5, False, UNKNOWN) != classify(True, -0.5, False, OFF)


class TestNoInferredPhysics:
  def test_no_pitch_or_measured_accel_inputs(self):
    # regression guard for the v3.3.9 mistake: classify takes the commanded
    # accel plus explicit reported-state flags — nothing derived from the IMU
    import inspect
    params = list(inspect.signature(classify).parameters)
    assert params == ['long_active', 'accel', 'gas_gating', 'brake_light']
    banned = ('pitch', 'coast', 'grade', 'measured', 'a_ego', 'aego')
    assert not any(b in p.lower() for p in params for b in banned)

  def test_no_coast_threshold_constant_survives(self):
    # _BRAKE_FIRM was a fixed coast-ish threshold standing in for "real
    # braking". The car's lamp replaced it; it must not creep back.
    assert not hasattr(_m, '_BRAKE_FIRM')
    assert not hasattr(_m, 'get_coast_accel')
