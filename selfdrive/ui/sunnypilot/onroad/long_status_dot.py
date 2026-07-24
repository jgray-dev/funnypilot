"""
FunnyPilot v3.3.9 — LongStatusDotRenderer: always-on longitudinal status dot,
bottom-left of the onroad screen.

  gray  = gas gating, coasting, or longitudinal deactivated (neither gas nor
          brakes actively applied)
  red   = braking, at any rate — mirrors brake lights
  green = gas/acceleration applied, at any extent

DISCRIMINATOR: a fixed accel deadband around zero does NOT work here — gas
gating clamps the commanded accel to roughly the natural COAST decel
(get_coast_accel() in selfdrive/controls/lib/longitudinal_planner.py returns
-0.3 m/s^2 on flat ground, more negative downhill), which is outside a naive
+-0.2 deadband and would misreport the gas-gating case as braking — exactly
the state this dot exists to disambiguate. The correct test (reviewed before
implementation) is the one the planner itself uses: braking means commanding
decel BEYOND what simply releasing the throttle would produce on this grade.
So this compares actuators.accel against the pitch-aware coast line, not a
fixed number.

get_coast_accel's one-line fit is duplicated here (not imported) to keep this
UI element decoupled from the control-loop module's heavier import chain —
same convention as eps_limit.py's duplicated Hyundai constants. Update the
two together if the fit ever changes.
"""
import math
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.widgets import Widget

_RADIUS = 14
_MARGIN = 24

_COLOR_GRAY = rl.Color(127, 140, 141, 220)
_COLOR_RED = rl.Color(231, 76, 60, 230)
_COLOR_GREEN = rl.Color(46, 204, 113, 230)
_COLOR_SHADOW = rl.Color(0, 0, 0, 90)

_BRAKE_EPS = 0.05    # m/s^2 past the coast line counts as genuine braking
_GAS_THRESHOLD = 0.1  # m/s^2, small threshold to reject pure actuator noise


def _coast_accel(pitch: float) -> float:
  # duplicated from selfdrive/controls/lib/longitudinal_planner.py:get_coast_accel
  return math.sin(pitch) * -5.65 - 0.3


def classify(long_active: bool, accel: float, pitch: float) -> str:
  """Pure classification, unit-tested in tests/test_long_status_dot.py.
  Returns 'gray' | 'red' | 'green'."""
  if not long_active:
    return 'gray'
  coast = _coast_accel(pitch)
  if accel < coast - _BRAKE_EPS:
    return 'red'
  if accel > _GAS_THRESHOLD:
    return 'green'
  return 'gray'


_COLOR_BY_STATE = {'gray': _COLOR_GRAY, 'red': _COLOR_RED, 'green': _COLOR_GREEN}


class LongStatusDotRenderer(Widget):
  def __init__(self):
    super().__init__()
    self._color = _COLOR_GRAY

  def _update_state(self) -> None:
    sm = ui_state.sm
    if sm.recv_frame["carControl"] < ui_state.started_frame:
      return

    cc = sm['carControl']
    pitch = cc.orientationNED[1] if len(cc.orientationNED) == 3 else 0.0
    state = classify(cc.longActive, cc.actuators.accel, pitch)
    self._color = _COLOR_BY_STATE[state]

  def _render(self, rect: rl.Rectangle) -> None:
    cx = int(rect.x + _MARGIN + _RADIUS)
    cy = int(rect.y + rect.height - _MARGIN - _RADIUS)
    rl.draw_circle(cx + 1, cy + 2, _RADIUS, _COLOR_SHADOW)
    rl.draw_circle(cx, cy, _RADIUS, self._color)
