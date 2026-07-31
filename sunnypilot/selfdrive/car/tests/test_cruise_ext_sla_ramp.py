"""FunnyPilot v3.4.5 — cruise_ext's half of the SLA set-speed ramp.

TWO THINGS ARE UNDER TEST, both of which were wrong before this version.

1. SINGLE WRITER. There used to be two unchained `if` blocks in
   update_speed_limit_assist_v_cruise_non_pcm, both assigning self.v_cruise_kph:
   a boundary snap to limit*(1+ratio) on a zone change, and the ramp follow.
   They ran on the same frame with the ramp LAST, so the snap's value was
   overwritten before it ever reached the car. It was dead code that read like
   the authoritative path — the most expensive kind. SLA now owns the value end
   to end, and a source guard here keeps the second writer from coming back.

2. DISPLAY-GRID QUANTIZATION. The driver's cruise buttons can only produce whole
   display units (1 kph metric, 1 mph = IMPERIAL_INCREMENT kph imperial). Writing
   a raw continuous target would park v_cruise_kph somewhere the driver could not
   have set it, and their next `+` press would snap to the nearest grid point,
   silently eating part of the change. Quantizing here is also what makes the
   ramp read on the cluster as a sequence of ordinary 1-mph taps.

Import-light: only the compiled params extension is stubbed.
"""
import ast
import pathlib
import sys
import types

import pytest


def _stub_compiled_deps():
  if 'openpilot.common.params' not in sys.modules:
    m = types.ModuleType('openpilot.common.params')

    class _Params:
      def __init__(self, *a, **k):
        pass

      def get(self, *a, **k):
        return 1

      def get_bool(self, *a, **k):
        return False

      def put(self, *a, **k):
        pass

    m.Params = _Params
    m.UnknownKeyName = type('UnknownKeyName', (Exception,), {})
    sys.modules['openpilot.common.params'] = m


_stub_compiled_deps()

try:
  from openpilot.sunnypilot.selfdrive.car import cruise_ext as ce
except ImportError as e:  # pragma: no cover - environment guard
  pytest.skip(f"unrelated compiled dependency unavailable: {e}", allow_module_level=True)

from cereal import car, custom
from openpilot.common.constants import CV

SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState
MPH = CV.MPH_TO_MS


def make_helper(is_metric=False):
  CP = car.CarParams.new_message(openpilotLongitudinalControl=True, pcmCruise=False)
  CP_SP = custom.CarParamsSP.new_message()
  h = ce.VCruiseHelperSP(CP, CP_SP)
  h.v_cruise_min = 20
  h.sla_state = SpeedLimitAssistState.active
  h.prev_sla_state = SpeedLimitAssistState.active
  h.sla_is_metric = is_metric
  return h


class TestDisplayGridQuantization:
  def test_imperial_target_lands_on_a_whole_mph(self):
    """MUTATION: write the raw target instead of rounding in display units.

    43.4 mph must become exactly the 43 mph grid point, i.e. 43 * 1.6 kph — the
    value a driver holding the `-` button would have produced.
    """
    h = make_helper()
    h.sla_v_cruise_target = 43.4 * MPH
    h.update_speed_limit_assist_v_cruise_non_pcm()
    assert abs(h.v_cruise_kph - 43 * ce.IMPERIAL_INCREMENT) < 1e-6

  def test_imperial_increment_matches_the_stock_cruise_helper(self):
    """The grid must be the SAME lattice selfdrive/car/cruise.py uses, or a
    driver press and a ramp write would sit on interleaved half-steps."""
    src = pathlib.Path(ce.__file__).parents[3] / 'selfdrive/car/cruise.py'
    tree = ast.parse(src.read_text())
    vals = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(getattr(t, 'id', None) == 'IMPERIAL_INCREMENT' for t in n.targets)]
    assert vals, "IMPERIAL_INCREMENT not found in cruise.py — this guard has gone stale"
    assert abs(ce.IMPERIAL_INCREMENT - eval(compile(ast.Expression(vals[0]), '<t>', 'eval'),
                                            {'CV': CV, 'round': round})) < 1e-9

  def test_metric_target_lands_on_a_whole_kph(self):
    h = make_helper(is_metric=True)
    h.sla_v_cruise_target = 63.4 * CV.KPH_TO_MS
    h.update_speed_limit_assist_v_cruise_non_pcm()
    assert abs(h.v_cruise_kph - 63.) < 1e-6

  def test_quantization_is_stable_frame_to_frame(self):
    """A target sitting between grid points must not dither between them."""
    h = make_helper()
    h.sla_v_cruise_target = 43.5 * MPH
    seen = set()
    for _ in range(10):
      h.update_speed_limit_assist_v_cruise_non_pcm()
      seen.add(round(float(h.v_cruise_kph), 6))
    assert len(seen) == 1


class TestWriteGating:
  def test_no_write_when_sla_inactive(self):
    h = make_helper()
    h.sla_state = SpeedLimitAssistState.inactive
    h.prev_sla_state = SpeedLimitAssistState.inactive
    h.v_cruise_kph = 99.
    h.sla_v_cruise_target = 43.4 * MPH
    h.update_speed_limit_assist_v_cruise_non_pcm()
    assert h.v_cruise_kph == 99.

  def test_no_write_when_the_channel_is_stale(self):
    """read_sla_shm returns 0.0 when plannerd is dead (see sla_shm.py). That
    must mean 'no request', not 'set speed zero'."""
    h = make_helper()
    h.v_cruise_kph = 99.
    h.sla_v_cruise_target = 0.
    h.update_speed_limit_assist_v_cruise_non_pcm()
    assert h.v_cruise_kph == 99.

  def test_button_press_holds_the_ramp_off(self):
    """MUTATION: delete the _ramp_hold_frames branch.

    Without the hold, the ramp overwrites the driver's press on the very next
    100 Hz frame and the press is lost before SLA can re-derive the ratio.
    """
    h = make_helper()
    h.v_cruise_kph = 99.
    h.sla_v_cruise_target = 43.4 * MPH
    CS = car.CarState.new_message()
    be = CS.init('buttonEvents', 1)
    be[0].type = ce.ButtonType.accelCruise
    be[0].pressed = True
    h.update_speed_limit_assist_v_cruise_non_pcm(CS)
    assert h.v_cruise_kph == 99.
    # ...and it resumes once the hold expires
    for _ in range(120):
      h.update_speed_limit_assist_v_cruise_non_pcm()
    assert abs(h.v_cruise_kph - 43 * ce.IMPERIAL_INCREMENT) < 1e-6

  def test_hold_outlasts_slas_own_intent_window(self):
    """v3.4.9. MUTATION: put the hold back to 100 frames (1 s), or shorten it
    below SLA's window.

    The two holds have to end together. SLA suspends its ramp for
    BUTTON_INTENT_FRAMES (0.5 s at 20 Hz) and adopts the driver's value there;
    this side must resume writing only AFTER that, or (too short) it overwrites
    the press before SLA has seen it, or (too long, the v3.4.5 state) SLA's ramp
    runs for half a second while nothing follows it and the cluster JUMPS when
    the hold finally expires.
    """
    from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import BUTTON_INTENT_FRAMES
    h = make_helper()
    CS = car.CarState.new_message()
    be = CS.init('buttonEvents', 1)
    be[0].type = ce.ButtonType.accelCruise
    be[0].pressed = True
    h.update_speed_limit_assist_v_cruise_non_pcm(CS)
    hold_s = (h._ramp_hold_frames + 1) / 100.        # this side runs at 100 Hz
    sla_window_s = BUTTON_INTENT_FRAMES * 0.05       # SLA runs at 20 Hz
    assert hold_s > sla_window_s
    assert hold_s < sla_window_s + 0.35, "a long overhang is what made the cluster jump"

  def test_target_is_clamped_to_the_cruise_range(self):
    h = make_helper()
    h.sla_v_cruise_target = 400. * MPH
    h.update_speed_limit_assist_v_cruise_non_pcm()
    assert h.v_cruise_kph == ce.V_CRUISE_MAX


class TestSingleWriter:
  """The structural guard. AST, not grep: the method's comment block describes
  the deleted second writer at length, so a substring scan would either match
  the prose or force it to be removed."""

  def _method(self):
    tree = ast.parse(pathlib.Path(ce.__file__).read_text())
    for node in ast.walk(tree):
      if isinstance(node, ast.FunctionDef) and node.name == 'update_speed_limit_assist_v_cruise_non_pcm':
        return node
    raise AssertionError("method not found — this guard has gone stale")

  def test_exactly_one_assignment_to_v_cruise_kph(self):
    """MUTATION: re-add the boundary snap as a second `if` writing v_cruise_kph."""
    n = 0
    for node in ast.walk(self._method()):
      if isinstance(node, ast.Assign):
        for t in node.targets:
          if isinstance(t, ast.Attribute) and t.attr == 'v_cruise_kph':
            n += 1
    assert n == 1, f"{n} writers of v_cruise_kph; the last one silently wins (see v3.4.4)"

  def test_the_writer_is_the_ramp_follow(self):
    """Anti-vacuity: one writer is only correct if it is the RIGHT one."""
    src = ast.unparse(self._method())
    assert 'sla_v_cruise_target' in src
