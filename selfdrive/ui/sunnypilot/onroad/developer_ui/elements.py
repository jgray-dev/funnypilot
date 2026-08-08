"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import time
import pyray as rl
from dataclasses import dataclass

from openpilot.common.constants import CV


from openpilot.system.ui.lib.text_measure import measure_text_cached


@dataclass
class UiElement:
  value: str
  label: str
  unit: str
  color: rl.Color
  val_text: str = ""
  label_text: str = ""
  unit_text: str = ""
  val_width: float = 0.0
  label_width: float = 0.0
  unit_width: float = 0.0
  total_width: float = 0.0

  def measure(self, font, font_size: int):
    self.label_text = f"{self.label} "
    self.val_text = self.value
    self.unit_text = f" {self.unit}" if self.unit else ""

    self.label_width = measure_text_cached(font, self.label_text, font_size, 0).x
    self.val_width = measure_text_cached(font, self.val_text, font_size, 0).x
    self.unit_width = measure_text_cached(font, self.unit_text, font_size, 0).x if self.unit else 0

    self.total_width = self.label_width + self.val_width + self.unit_width


# ════════════════════════════════════════════════════════════════════════════
# FunnyPilot v3.6.2 — THE DEV UI IS NOW SCC-M v2's INSTRUMENT PANEL.
#
# Everything that used to be here (EPS/LIM/TBAR/BUMP, the lat-accel pair, the
# steering readouts) belonged to the v3.3.8 turn-in oscillation investigation,
# which closed. What replaces it is the set of numbers that answer "is SCC-M v2
# doing the right thing" on a drive, and each one is chosen to make a SPECIFIC
# failure visible rather than to be generally informative:
#
#   CORN 0 on a road with bends      -> the geometry is not finding them
#   R far from the bend you can see  -> the radius estimator is wrong here
#   CVSP sensible but CAP unmoving   -> the fusion is vetoing it
#   GATE never on                    -> the approach will be a late brake
#   PASS never increments            -> nothing is being learned; the observer
#                                       is failing, not the budget
#   VIS 0 forever on your commute    -> passes are committing somewhere else
#
# All of it comes from ONE /dev/shm read per frame (scc_shm.read_scc_debug_shm),
# because the alternative is the UI recomputing a controller's decisions and
# disagreeing with it.
# ════════════════════════════════════════════════════════════════════════════

def _scc_debug():
  """One read per frame, shared by every SCC element below."""
  global _SCC_CACHE, _SCC_CACHE_T
  now = time.monotonic()
  if now - _SCC_CACHE_T > 0.05:
    try:
      from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import read_scc_debug_shm
      _SCC_CACHE = read_scc_debug_shm()
    except Exception:
      _SCC_CACHE = (0, 0.0, 0.0, 0.0, 0.0, 0, False, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0)
    _SCC_CACHE_T = now
  return _SCC_CACHE


_SCC_CACHE = (0, 0.0, 0.0, 0.0, 0.0, 0, False, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0)
_SCC_CACHE_T = 0.0

_GREY = rl.Color(0x9A, 0xA6, 0xB2, 255)
_GREEN = rl.Color(0x2E, 0xC5, 0x8B, 255)
_AMBER = rl.Color(0xFF, 0xB4, 0x54, 255)
_RED = rl.Color(0xE5, 0x4B, 0x4B, 255)
_CYAN = rl.Color(0x4A, 0xC8, 0xE0, 255)


class SccCornersElement:
  """How many corners the geometry currently sees. ZERO on a road you can see
  bends on is the single most diagnostic reading on this screen: it says the
  polyline is missing, the turn gate is too strict, or mapd has no route."""
  def update(self, sm, is_metric):
    n = _scc_debug()[0]
    return UiElement(str(n), "CORN", "", _GREY if n == 0 else _GREEN)


class SccRadiusElement:
  """Measured radius of the corner we are braking for. Compare it with the bend
  out of the windscreen — that comparison is the whole validation of
  road_geometry, which has only ever been tested against synthetic data."""
  def update(self, sm, is_metric):
    r = _scc_debug()[1]
    if r <= 0:
      return UiElement("-", "R", "", _GREY)
    return UiElement(f"{r:.0f}", "R", "m", _CYAN)


class SccCornerSpeedElement:
  """The speed we chose for that corner: sqrt(a_lat * R), before the approach
  envelope. This is the number to argue with — it is what the car thinks the
  bend is worth."""
  def update(self, sm, is_metric):
    v = _scc_debug()[2]
    if v <= 0:
      return UiElement("-", "CVSP", "", _GREY)
    conv = CV.MS_TO_KPH if is_metric else CV.MS_TO_MPH
    return UiElement(f"{v * conv:.0f}", "CVSP", "km/h" if is_metric else "mph", _CYAN)


class SccDistanceElement:
  """How far away it is, along the road. Watch this fall smoothly: a value that
  jumps means the route re-matched or the corner list churned."""
  def update(self, sm, is_metric):
    d = _scc_debug()[3]
    if d <= 0:
      return UiElement("-", "DIST", "", _GREY)
    return UiElement(f"{d:.0f}", "DIST", "m", _GREY)


class SccALatElement:
  """The lateral budget in force for that corner — the DEFAULT until the corner
  has been driven, then the learned value blended in by visit count. Seeing
  this move away from 1.80 is seeing the feature work."""
  def update(self, sm, is_metric):
    a = _scc_debug()[4]
    if a <= 0:
      return UiElement("-", "ALAT", "", _GREY)
    col = _GREY if abs(a - 1.8) < 0.05 else (_GREEN if a > 1.8 else _AMBER)
    return UiElement(f"{a:.2f}", "ALAT", "", col)


class SccVisitsElement:
  """Visits to the governing corner. 0 = geometry only. On your commute this
  should reach 3 within a week; if it stays 0 the passes are being committed
  somewhere other than where the lookup is asking."""
  def update(self, sm, is_metric):
    n = _scc_debug()[5]
    return UiElement(str(n), "VIS", "", _GREY if n == 0 else _GREEN)


class SccGateElement:
  """The gas gate. ON means the planner is holding the throttle closed because
  a corner ahead will need us slower. It should come on WELL before the cap
  starts falling — that lead time is the difference between coasting into a
  bend and braking into one."""
  def update(self, sm, is_metric):
    on = _scc_debug()[6]
    return UiElement("ON" if on else "off", "GATE", "", _AMBER if on else _GREY)


class SccCapElement:
  """What SCC-M v2 is actually asking the car to hold, after the envelope, the
  smoothing and the set-speed clamp."""
  def update(self, sm, is_metric):
    v = _scc_debug()[7]
    if v <= 0:
      return UiElement("-", "CAP", "", _GREY)
    conv = CV.MS_TO_KPH if is_metric else CV.MS_TO_MPH
    return UiElement(f"{v * conv:.0f}", "CAP", "km/h" if is_metric else "mph", _AMBER)


class SccAuthorityElement:
  """How much of the cut survived the corroboration gate. 100 = passed whole,
  0 = vetoed. A sensible CVSP with authority 0 means the fusion is the thing
  stopping the car slowing, not the geometry."""
  def update(self, sm, is_metric):
    a = _scc_debug()[8]
    pct = int(round(a * 100))
    col = _GREEN if pct >= 99 else (_AMBER if pct > 0 else _RED)
    return UiElement(f"{pct}", "AUTH", "%", col)


class SccLearnedElement:
  """Total corners in the store."""
  def update(self, sm, is_metric):
    n = _scc_debug()[9]
    return UiElement(str(n), "LRN", "", _GREY if n == 0 else _GREEN)


class SccLastPassElement:
  """The last committed pass: peak lateral g and how far past the limits it
  was. Severity under 0.5 raised that corner's floor; over 1.0 lowered its
  ceiling; in between it changed nothing, which is the honest answer more often
  than not."""
  def update(self, sm, is_metric):
    a_peak, sev = _scc_debug()[10], _scc_debug()[11]
    if a_peak <= 0:
      return UiElement("-", "PASS", "", _GREY)
    col = _GREEN if sev < 0.5 else (_RED if sev >= 1.0 else _AMBER)
    return UiElement(f"{a_peak:.1f}/{sev:.1f}", "PASS", "", col)


class SccLaneDepartElement:
  """FunnyPilot v3.6.4 — how far outside the lane the last pass got, in cm.

  THE MOST DIRECT OF THE FOUR SIGNALS. The other three ask how hard the
  controller had to work; this one asks whether the car actually stayed where
  it belonged. Anything non-zero means a wheel was over a line during the
  bend, which is the plainest possible evidence the speed was too high — and
  it is what the driver notices on a blind corner that turns out tighter than
  it looked.

  Green at zero, amber inside the line-touching band, red once it is past
  DEPART_LIMIT_M (25 cm), which is the point at which the departure alone
  drives severity to 1.0 and lowers that corner's ceiling.
  """
  def update(self, sm, is_metric):
    d = _scc_debug()[14]
    if d <= 0.0:
      return UiElement("0", "LANE", "cm", _GREEN)
    col = _RED if d >= 0.25 else _AMBER
    return UiElement(f"{d * 100:.0f}", "LANE", "cm", col)


class SccPassCountElement:
  """Passes committed this drive. IF THIS STAYS 0 THE FEATURE IS NOT LEARNING,
  and the fault is in the observer rather than in any budget or threshold —
  which is exactly the distinction v3.6.1 spent a release failing to make."""
  def update(self, sm, is_metric):
    n = _scc_debug()[13]
    return UiElement(str(n), "NPAS", "", _GREY if n == 0 else _GREEN)
