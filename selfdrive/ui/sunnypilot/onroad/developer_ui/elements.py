"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import math
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

# THE RESTING PAYLOAD IS THE PUBLISHER'S OWN, NOT A COPY OF IT, and v3.6.7 is
# why that matters rather than being tidy. The fallback here used to be a
# hand-typed 16-tuple; this release appended four fields, so every element
# reading index 16..19 would have raised IndexError — AT 60 Hz, IN THE UI
# PROCESS — the moment the lazy import failed for any reason. A hand-written
# copy of a wire format cannot notice that the format grew. That is the same
# defect as v3.6.3's five-name unpack, one layer further out.
#
# `test_dev_panel.py` pins the length against the highest index anything reads,
# so the next field added fails a test rather than a boot.
try:
  from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import DEBUG_INACTIVE as _SCC_INACTIVE
except Exception:
  _SCC_INACTIVE = (0, 0.0, 0.0, 0.0, 0.0, 0, False, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0, 0.0, 0,
                   0.0, 0.0, 0.0, 0)


def _scc_debug():
  """One read per frame, shared by every SCC element below."""
  global _SCC_CACHE, _SCC_CACHE_T
  now = time.monotonic()
  if now - _SCC_CACHE_T > 0.05:
    try:
      from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import read_scc_debug_shm
      _SCC_CACHE = read_scc_debug_shm()
    except Exception:
      _SCC_CACHE = _SCC_INACTIVE
    _SCC_CACHE_T = now
  return _SCC_CACHE


_SCC_CACHE = _SCC_INACTIVE
_SCC_CACHE_T = 0.0

_GREY = rl.Color(0x9A, 0xA6, 0xB2, 255)
_GREEN = rl.Color(0x2E, 0xC5, 0x8B, 255)
_AMBER = rl.Color(0xFF, 0xB4, 0x54, 255)
_RED = rl.Color(0xE5, 0x4B, 0x4B, 255)
_CYAN = rl.Color(0x4A, 0xC8, 0xE0, 255)

# FunnyPilot v3.6.7 — TAKEN FROM THE CONTROLLERS, NOT RETYPED. The veto
# threshold and the source names are the controllers' own, and a debug panel
# that carries its own copy of either becomes wrong the first time one is
# tuned. Imported inside a try because nothing in a UI module may raise at
# import — `selfdrive/ui/ui.py` builds every layout up front, so an import
# error here is a boot loop on a device whose settings screen is how you would
# flash out of it.
#
# THE FALLBACKS ARE FOR COLOUR ONLY. If the import fails, CORR still shows the
# real published number and SRC still shows a code; only the green/amber split
# and the label are advisory. That is the right direction to fail: a readout
# that is honest but uncoloured beats one that is confidently wrong.
try:
  from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import VISION_DISAGREE_TH as _VETO_TH
except Exception:
  _VETO_TH = 0.30
try:
  from openpilot.selfdrive.controls.lib.long_shaping import SRC_NAMES as _SRC_NAMES
except Exception:
  _SRC_NAMES = {}


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
  stopping the car slowing, not the geometry.

  v3.6.7 — "-" IN GREY WITH NO UNIT WHEN THERE IS NO CORNER, matching every
  other element in this column. It used to read a RED 0%, which is the same
  glyph it uses for "vetoed" — so the bottom of the column shouted a fault on
  every straight road, and the one reading that means something was
  indistinguishable from the resting state. Gated on the SAME test CVSP uses
  (field 2, the governing corner's own speed), so the column cannot show an
  authority for a corner it is not showing a speed for."""
  def update(self, sm, is_metric):
    if _scc_debug()[2] <= 0:
      return UiElement("-", "AUTH", "", _GREY)
    pct = int(round(_scc_debug()[8] * 100))
    col = _GREEN if pct >= 99 else (_AMBER if pct > 0 else _RED)
    return UiElement(f"{pct}", "AUTH", "%", col)


class SccLearnedElement:
  """FunnyPilot v3.6.7 — THE LEARNING SIDE AS ONE ELEMENT: store / passes this
  drive / orphans.

  Three separate readouts (LRN, NPAS, ORPH) became one because the bottom bar
  divides a fixed width between however many elements it is given, so every
  element added shrinks every other one. These three are read TOGETHER anyway —
  the question is always "is the store filling, from passes, and is the geometry
  keeping up" — and the PASS element already established that a slash-joined
  pair reads fine at this size.

  store 0 with passes climbing means commits are being rejected; orphans
  climbing on familiar roads means the radius estimator is the weak link (the
  documented short-sweep blind spot), not the budget.
  """
  def update(self, sm, is_metric):
    store, npas, orph = _scc_debug()[9], _scc_debug()[13], _scc_debug()[15]
    col = _GREY if store == 0 else (_AMBER if orph > 0 else _GREEN)
    return UiElement(f"{store}/{npas}/{orph}", "LRN", "", col)


# ════════════════════════════════════════════════════════════════════════════
# FunnyPilot v3.6.7 — THE LONGITUDINAL ROW.
#
# v3.6.7 moved three things that decide how this car brakes and follows, and
# none of them were visible from the seat:
#
#   * the actuator layer's tracking of the planner's demand — the pure-P lag
#     that put the car 0.27/0.54/0.80 m/s^2 behind a 1/2/3 m/s^3 ramp
#   * the stop-and-go governor, and its v3.6.6 launch bug
#   * which constraint is actually binding, out of five that can be
#
# Each element below names a SPECIFIC failure the way the SCC ones do:
#
#   TRK large during a brake ramp  -> the feed-forward is not working; the
#                                     actuator is behind the plan again
#   SRC CRZ while closing on a car -> the lead obstacle is not binding
#   SRC flapping LEAD<->CRZ        -> the following wave, seen directly
#   STOP off approaching a queue   -> the stop governor is not engaging
#   STOP on with the lead pulling  -> the v3.6.6 launch bug is back
#     away
#   CORR high on a straight road   -> the vision veto cannot fire; SCC-M keeps
#                                     its authority over bad map data
# ════════════════════════════════════════════════════════════════════════════

# How fast the tracking readout follows. The quantity of interest is the
# SUSTAINED error on a ramp, not the legitimate transient after a step, so this
# is a low pass rather than the max-hold the old EPS/TBAR readouts used — a
# max-hold would show the step response, which is the lag doing its job.
_TRK_TAU = 0.4
_TRK_DT_MAX = 0.25   # a stalled frame must not teleport the filter


class LongTrackingElement:
  """How far the actuator's output is behind the planner's command, m/s^2.

  **THE ONE NUMBER THAT VALIDATES v3.6.7.** Both sides are published and
  neither is re-derived here: `carControl.actuators.accel` is literally what
  the controller assigns to `accel_cmd`, and `carOutput.actuatorsOutput.accel`
  is literally what `jerk_limited_integrator` produced from it. The difference
  IS the lag this release removes — a measurement, not a copy of the maths.

  Before the fix a 2 m/s^3 brake ramp sat at 0.54 and stayed there for as long
  as the ramp lasted. After it, near zero.

  KNOWN AND DELIBERATELY NOT COMPENSATED: the controller clips its command to
  [ACCEL_MIN, ACCEL_MAX] before the integrator, so a demand past -3.5 shows the
  clip here as well as the lag. Clipping the same way in the UI would be
  re-deriving a controller constant, which is the thing this file does not do.
  A TRK pinned at exactly the overshoot past -3.5 is the clip, not the law.
  """
  def __init__(self):
    self._v = 0.0
    self._t = 0.0

  def update(self, sm, is_metric):
    try:
      active = bool(sm['carControl'].longActive)
      cmd = float(sm['carControl'].actuators.accel)
      out = float(sm['carOutput'].actuatorsOutput.accel)
    except Exception:
      active, cmd, out = False, 0.0, 0.0
    now = time.monotonic()
    dt = min(max(now - self._t, 0.0), _TRK_DT_MAX) if self._t else 0.0
    self._t = now
    if not active:
      self._v = 0.0
      return UiElement("-", "TRK", "", _GREY)
    err = abs(out - cmd)
    if err != err:  # NaN
      err = 0.0
    self._v += (err - self._v) * (1.0 - math.exp(-dt / _TRK_TAU)) if dt > 0 else 0.0
    col = _GREEN if self._v < 0.10 else (_AMBER if self._v < 0.30 else _RED)
    return UiElement(f"{self._v:.2f}", "TRK", "", col)


class LongAccelElement:
  """The accel the planner is asking for, m/s^2. TRK's denominator, in effect:
  a large TRK against a flat ACC is a different fault from a large TRK against
  a moving one, and only the second is the tracking lag."""
  def update(self, sm, is_metric):
    try:
      if not bool(sm['carControl'].longActive):
        return UiElement("-", "ACC", "", _GREY)
      a = float(sm['carControl'].actuators.accel)
    except Exception:
      return UiElement("-", "ACC", "", _GREY)
    col = _GREY if abs(a) < 0.05 else (_GREEN if a > 0 else _AMBER)
    return UiElement(f"{a:+.2f}", "ACC", "", col)


class LongSourceElement:
  """WHICH CONSTRAINT IS BINDING, classified by the planner (long_shaping.
  long_source_code) rather than re-derived here.

  Watch it behind a lead: it should sit on LEAD. Flapping LEAD <-> CRZ is the
  following wave itself — the gap opening far enough that the lead obstacle
  stops being the nearest one, which is exactly the loop v3.6.7 set out to
  close."""
  def update(self, sm, is_metric):
    code = _scc_debug()[19]
    name = _SRC_NAMES.get(code, "?")
    col = _GREY if code == 0 else (_CYAN if code == 1 else _AMBER)
    return UiElement(name, "SRC", "", col)


class StopGovernorElement:
  """The stop-and-go governor's cap. `off` when it is not constraining.

  ON approaching a stopped queue is the v3.6.6 feature working. ON while the
  lead is PULLING AWAY is the v3.6.6 launch bug, which v3.6.7 fixed by only
  acting while the gap is actually shrinking — if it ever reads a live number
  as the car fails to follow a departing lead, the closing gate has regressed.
  """
  def update(self, sm, is_metric):
    v = _scc_debug()[18]
    if v <= 0:
      return UiElement("off", "STOP", "", _GREY)
    conv = CV.MS_TO_KPH if is_metric else CV.MS_TO_MPH
    return UiElement(f"{v * conv:.0f}", "STOP", "km/h" if is_metric else "mph", _AMBER)


class LeadElement:
  """Distance to the lead and our closing rate, m and m/s.

  Not a v3.6.7 change, but the following wave cannot be read without it: the
  symptom is a gap that oscillates, and the gap is this number."""
  def update(self, sm, is_metric):
    try:
      lead = sm['radarState'].leadOne
      if not lead.status:
        return UiElement("-", "LEAD", "", _GREY)
      d, vrel = float(lead.dRel), float(lead.vRel)
    except Exception:
      return UiElement("-", "LEAD", "", _GREY)
    col = _RED if d < 12.0 else (_AMBER if vrel < -1.5 else _GREEN)
    return UiElement(f"{d:.0f}/{vrel:+.0f}", "LEAD", "", col)


class SccVisionCapElement:
  """SCC-V's OWN cap, beside SCC-M's. v3.6.7.

  The whole point of this release's fusion change is that the two can disagree
  and vision wins where vision is looking, so the panel has to show both
  numbers or there is nothing to compare. `-` means SCC-V has nothing to say
  here — which, with CORR also low, is the state in which SCC-M is now vetoed.
  """
  def update(self, sm, is_metric):
    v = _scc_debug()[16]
    if v <= 0:
      return UiElement("-", "SCCV", "", _GREY)
    conv = CV.MS_TO_KPH if is_metric else CV.MS_TO_MPH
    return UiElement(f"{v * conv:.0f}", "SCCV", "km/h" if is_metric else "mph", _CYAN)


class SccCorroborationElement:
  """The model's own reading of the road ahead — the number the vision veto is
  compared against, shown raw. v3.6.7.

  It is the lateral acceleration the model's PATH would pull at our current
  speed, over 1.05 m/s^2. So 1.00 is a real corner and 0.00 is a road the model
  says is straight. `VISION_DISAGREE_TH` is 0.30, and **below that SCC-M v2 has
  no authority at all where the model can see** — which is the reported
  lane-merge case.

  READ IT ON A STRAIGHT ROAD FIRST. This is what v3.6.7 re-tuned and the tuning
  is the falsifiable part: if it idles ABOVE 0.30 on plain straight road, the
  veto still cannot fire and the threshold is what needs to move — not the
  budget, and not the geometry. Green below the threshold is "vision would veto
  here"; amber above it is "vision agrees there is something".
  """
  def update(self, sm, is_metric):
    c = _scc_debug()[17]
    col = _GREEN if c < _VETO_TH else _AMBER
    return UiElement(f"{c:.2f}", "CORR", "", col)


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

  THE MOST DIRECT OF THE CONTROLLER-SIDE SIGNALS. The other three ask how hard
  the controller had to work; this one asks whether the car actually stayed
  where it belonged. Anything non-zero means a wheel was over a line during the
  bend, which is the plainest possible evidence the speed was too high — and
  it is what the driver notices on a blind corner that turns out tighter than
  it looked.

  Green at zero, amber inside the line-touching band, red once it is past
  DEPART_LIMIT_M (25 cm), which is the point at which the departure alone
  drives severity to 1.0 and lowers that corner's ceiling.

  v3.6.5 — A PERMANENT 0 HERE WAS A BUG, NOT A CLEAN DRIVE, and it read that
  way for the whole of v3.6.4: `lane_departure_m` was written for a
  y-positive-LEFT model frame and openpilot's is y-positive-RIGHT, so the lane
  width came out negative, the plausibility gate rejected every frame, and the
  signal returned the same 0.0 it returns for "inside the lane". If it ever
  goes permanently green again on a drive that included a wide line, suspect
  the gates before the geometry — a signal whose failure mode is its own
  healthy reading needs the departure to be seen non-zero at least once.

  A takeover has NO row of its own on purpose: it lands in PASS as a severity
  of TAKEOVER_SEVERITY (2.0) or more, which already reads red, and widening the
  debug wire format again for something the existing readout says is how a
  positional channel accumulates fields nobody looks at.
  """
  def update(self, sm, is_metric):
    d = _scc_debug()[14]
    if d <= 0.0:
      return UiElement("0", "LANE", "cm", _GREEN)
    col = _RED if d >= 0.25 else _AMBER
    return UiElement(f"{d * 100:.0f}", "LANE", "cm", col)
