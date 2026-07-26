"""
FunnyPilot v3.4.4 — LongStatusDotRenderer: always-on longitudinal status dot,
bottom-left of the onroad screen.

  red   = THE CAR'S BRAKE LIGHTS ARE ON (brakes actually being applied)
  green = throttle commanded
  gray  = everything else: gas gating, coasting / off throttle, or long
          control not active

v3.4.0-3.4.3 called ANY commanded deceleration red. That reads wrong on the
road and the user said so plainly: lifting to a lower throttle is not braking,
but the dot went red anyway. `aReqValue < 0` is a request to slow down; on this
platform the ESC decides whether to satisfy it by cutting throttle or by
pressing the brakes, so the sign of the command simply does not answer the
question "are my brake lights on".

WHAT IT READS NOW, and why that is not a regression to v3.3.9. Red comes from
`TCS13.BrakeLight`, a bit the ESC broadcasts about its own actuator: lamps lit
or not. It is a reported control state, not a derived physical quantity —
there is no threshold, no coast line, no road-grade term, nothing from the IMU.
That distinction is the whole point of the v3.3.9 post-mortem: v3.3.9 compared
the commanded accel against `get_coast_accel(pitch)`, i.e. it INFERRED braking
from estimated physics. Asking the car is the opposite of inferring. See
sunnypilot/selfdrive/car/brake_light_shm.py for how the bit crosses processes
(and why it is a /dev/shm file rather than a capnp field).

Green stays on the commanded side: `carOutput.actuatorsOutput.accel` > 0 is the
literal value packed into SCC12's `aReqValue` (opendbc hyundai carcontroller:
`new_actuators.accel = self.tuning.actual_accel`), i.e. the last software layer
before the car, and it means "we are asking for throttle". KNOWN ASYMMETRY,
deliberate: the car publishes a brake lamp but no equally unambiguous "throttle
applied" bit, so a steady-state cruise that holds speed with real throttle but a
~zero accel command reads gray rather than green. Fixing that honestly would
need an engine-torque signal (EMS16.TQI / TCS13.TQI_SCC) whose "any throttle"
boundary is not obvious; do not paper over it with another threshold.

Gas gating is still reported as gray from the control code's OWN published
flags (SLA's pre-zone gate, the SCC-V/SCC-M curve gates), never by recognizing
a coast-shaped accel value. A lit brake lamp outranks a gate flag, so a real
brake application during an approach is never masked gray.
"""
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.selfdrive.car.brake_light_shm import read_brake_light
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
_EPS = 0.02  # m/s^2


def classify(long_active: bool, accel: float, gas_gating: bool, brake_light: bool | None) -> str:
  """Pure classification, unit-tested in tests/test_long_status_dot.py.

  brake_light is the car's own lamp bit: True/False when card is publishing it,
  None when it is UNKNOWN (not a Hyundai classic-CAN car, card not running, or
  a stale channel). None is handled explicitly and degrades to the old
  commanded-decel rule rather than silently claiming the brakes are off.
  """
  if not long_active:
    return 'gray'
  if brake_light:
    return 'red'      # the car says its brake lamps are lit; that outranks everything
  if gas_gating:
    return 'gray'     # deliberate throttle hold-off, per the controllers' own flags
  if brake_light is None and accel < -_EPS:
    return 'red'      # degraded fallback: no lamp bit available, fall back to the command
  if accel > _EPS:
    return 'green'
  return 'gray'       # off throttle / holding — reduced throttle is NOT braking


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
    # /dev/shm (v3.4.1 — see sla_shm.py, which explains why no capnp field).
    gas_gating = False
    try:
      scc = sm['longitudinalPlanSP'].smartCruiseControl
      gas_gating = bool(scc.vision.gasGating or scc.map.gasGating)
    except Exception:
      pass
    if not gas_gating:
      _, gas_gating = read_sla_shm()

    # v3.4.4: the car's own brake-lamp bit, published by card (see
    # sunnypilot/selfdrive/car/brake_light_shm.py). None = unknown, not False.
    brake_light = read_brake_light()

    self._color = _COLOR_BY_STATE[classify(long_active, accel, gas_gating, brake_light)]

  def _render(self, rect: rl.Rectangle) -> None:
    cx = int(rect.x + _MARGIN + _RADIUS)
    cy = int(rect.y + rect.height - _MARGIN - _RADIUS)
    rl.draw_circle(cx + 1, cy + 2, _RADIUS, _COLOR_SHADOW)
    rl.draw_circle(cx, cy, _RADIUS, self._color)
