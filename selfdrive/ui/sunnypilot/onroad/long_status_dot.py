"""
FunnyPilot v3.4.0 — LongStatusDotRenderer: always-on longitudinal command dot,
bottom-left of the onroad screen.

  gray  = neither gas nor brakes commanded (gas gating, coasting, or long
          control not active)
  red   = deceleration commanded, at any rate
  green = acceleration commanded, at any extent

WHAT IT READS, and why: `carOutput.actuatorsOutput.accel`. That is not an
estimate and not a measurement — for this car it is the exact value the
carcontroller packs into SCC12's `aReqValue` (see
opendbc/car/hyundai/carcontroller.py: `new_actuators.accel =
self.tuning.actual_accel`, and hyundaican.create_acc_commands). In other
words it is the literal last software layer between openpilot and the car:
the number that says "apply throttle" or "apply braking".

v3.3.9 got this wrong by comparing that command against a PITCH-DERIVED coast
estimate (get_coast_accel) to decide what counted as braking. That made the
dot a function of IMU-derived road grade — inferred physics, exactly the
"acceleration sensing" this readout is supposed to avoid. It is gone: the
only inputs now are the commanded accel and explicit control-state booleans.

Gas gating is reported as gray via the control code's OWN published flags
(SLA's pre-zone gate and the SCC-V/SCC-M curve gates), not by trying to
recognize a coast-shaped accel value. When a gate is holding the throttle
off, the command rides at whatever the planner clamped it to; the flag is
what tells us that is a deliberate "no gas", not braking. Explicit commanded
braking still wins over a gate flag, so a real brake application during an
approach is never masked.
"""
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.sla_shm import read_sla_shm
from openpilot.system.ui.widgets import Widget

_RADIUS = 14
_MARGIN = 24

_COLOR_GRAY = rl.Color(127, 140, 141, 220)
_COLOR_RED = rl.Color(231, 76, 60, 230)
_COLOR_GREEN = rl.Color(46, 204, 113, 230)
_COLOR_SHADOW = rl.Color(0, 0, 0, 90)

# Deadband purely for float/actuator noise around a zero command — NOT a
# physical model of anything.
_EPS = 0.02          # m/s^2
_BRAKE_FIRM = -0.35  # m/s^2: unambiguous braking, reported even while a gate flag is up


def classify(long_active: bool, accel: float, gas_gating: bool) -> str:
  """Pure classification, unit-tested in tests/test_long_status_dot.py."""
  if not long_active:
    return 'gray'
  if accel < _BRAKE_FIRM:
    return 'red'      # real braking always wins over a gate flag
  if gas_gating:
    return 'gray'     # deliberate throttle hold-off, per the controllers' own flags
  if accel < -_EPS:
    return 'red'
  if accel > _EPS:
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

    # the commanded accel actually sent to the car (post jerk-limiting), not
    # the planner's request and not anything measured
    accel = sm['carOutput'].actuatorsOutput.accel
    long_active = sm['carControl'].longActive

    # SCC-V/SCC-M gate flags are already in the schema; SLA's comes over
    # /dev/shm (v3.4.1 — see sla_shm.py; adding it to capnp is what forced the
    # device rebuild that broke the 3.4.0 boot).
    gas_gating = False
    try:
      scc = sm['longitudinalPlanSP'].smartCruiseControl
      gas_gating = bool(scc.vision.gasGating or scc.map.gasGating)
    except Exception:
      pass
    if not gas_gating:
      _, gas_gating = read_sla_shm()

    self._color = _COLOR_BY_STATE[classify(long_active, accel, gas_gating)]

  def _render(self, rect: rl.Rectangle) -> None:
    cx = int(rect.x + _MARGIN + _RADIUS)
    cy = int(rect.y + rect.height - _MARGIN - _RADIUS)
    rl.draw_circle(cx + 1, cy + 2, _RADIUS, _COLOR_SHADOW)
    rl.draw_circle(cx, cy, _RADIUS, self._color)
