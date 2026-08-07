"""FunnyPilot v3.6.2 — how hard did the car have to work to get round that?

SCC-M v2's definition of the ideal corner speed is the one that was asked for:
AS FAST AS POSSIBLE WITHOUT lateral oscillation, without the driver-torque
clamp cutting our steering request, and without the steering controller hitting
its limits. This module turns those three into a number, per pass, so the
answer is measured rather than inferred from how the car "felt".

────────────────────────────────────────────────────────────────────────────
THE THREE SIGNALS, AND WHERE EACH ONE COMES FROM

  oscillation     `steeringAngleDeg`, high-passed. The steady ramp of steering
                  into a bend is removed; what is left is correction. Counting
                  amplitude-qualified sign changes gives reversals per second,
                  which is a rate the driver would describe as "sawing".
                  AVAILABLE IN BOTH REGIMES, which is what makes requirement 4
                  work — a driver fighting a corner looks the same as a
                  controller fighting one.

  torque clamp    `|carState.steeringTorque|` against TBAR_LIMIT. The K5's
                  driver-torque clamp starts reducing our authority at a sensor
                  reading of 50 (eps_limit.py), well below the 150 that
                  latches `steeringPressed`. ONLY COUNTED WHILE LATERAL IS
                  ACTIVE: with the driver steering, a high reading is the
                  driver driving, not a limit being hit.

  steering limits `controlsState.lateralControlState.torqueState.saturated`,
                  and the EPS governor's own `limited` fraction published on
                  /dev/shm/lat_interp. Both are meaningless with lateral off,
                  and both are ignored there.

────────────────────────────────────────────────────────────────────────────
SEVERITY IS CONTINUOUS, AND THAT IS WHAT ANSWERS "HOW FAR PAST"

A pass does not report "stressed" or "not stressed"; it reports how many times
over the limit it was. Drive a bend far too fast with nothing engaged and the
reversal rate comes out at three times the threshold, so the pass says the
corner supports a third of the lateral acceleration that was just pulled —
which is exactly the guess that was asked for, and it is arithmetic rather than
a guess. `severity` below CLEAN_TH is positive evidence the corner supports
what was done; between CLEAN_TH and 1.0 it is no evidence either way, which is
an honest thing for a measurement to say.

WHAT IS **NOT** EXCLUDED, AND WHY THAT IS AN IMPROVEMENT. v3.5.0's observer had
to exclude leads, stops, zone changes and SLA activity, because it inferred a
corner from a speed dip and all of those produce dips. This one is told where
the corners are by the road geometry, and it measures lateral acceleration,
which traffic does not produce. Following a slow car round a bend at 0.8 m/s^2
is simply a pass with a low peak: it fails to raise the floor and changes
nothing. The interval cannot be poisoned by something that did not happen
laterally.

Import-light (stdlib only).
"""
import math

# ── oscillation ────────────────────────────────────────────────────────────
# The high-pass time constant. Long enough to pass a 1-4 Hz correction
# untouched, short enough that the steady steering ramp into a bend — which can
# take a second or two — is removed and does not read as one enormous
# excursion.
OSC_TAU_S = 0.60
# A reversal only counts if the excursion since the last one exceeded this. On
# the wheel that is a visible correction, not sensor noise or road camber.
OSC_AMP_DEG = 1.5
# Reversals per second at which the pass is exactly at the limit. Roughly 0.8
# Hz of sustained correction — noticeable from the seat, and well above what a
# smoothly driven bend produces.
OSC_RATE_LIMIT = 1.6

# ── the driver-torque clamp ────────────────────────────────────────────────
# eps_limit.py's model of the K5 rack: allowed torque starts falling once the
# driver-torque sensor passes 50. Native CAN units, same as CS.steeringTorque.
TBAR_LIMIT = 50.0

# Fraction of the pass spent against a hard limit (clamp, saturation, or the
# EPS governor actually biting) at which the pass is exactly at the limit.
LIMIT_FRAC_LIMIT = 0.15

# Below this, the pass is positive evidence that the corner supports what was
# done. Between here and 1.0 it is evidence of nothing.
CLEAN_TH = 0.5
# A single pass may not claim the corner is worse than this multiple over.
# Without it one bad sample (a pothole mid-bend, a swerve) could drive the
# ceiling to the floor in one visit; the interval is supposed to close, not
# collapse.
MAX_SEVERITY = 3.0

MIN_PASS_S = 1.0       # shorter than this and the rate statistics mean nothing
MIN_PASS_V = 5.0       # m/s; below this the lateral signals are not informative


def _finite(x) -> bool:
  try:
    f = float(x)
  except (TypeError, ValueError):
    return False
  return f == f and abs(f) != float('inf')


class LateralEffort:
  """Per-frame lateral signals, stateful only in the oscillation high-pass.

  Fed at the carState rate (100 Hz in plannerd's poll loop). Everything it
  exposes describes THIS frame; accumulating across a corner is CornerPass's
  job, so this object can be tested one frame at a time.
  """

  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self._ema = None
    self._sign = 0
    self._peak = 0.0
    self.reversal = False      # a qualified steering reversal happened this frame
    self.a_lat = 0.0           # measured lateral acceleration, m/s^2
    self.limited = False       # a hard lateral limit is being hit this frame

  def update(self, dt: float, v_ego: float, curvature: float, steering_angle_deg: float,
             steer_torque: float, lat_active: bool, saturated: bool = False,
             eps_limited: bool = False) -> None:
    self.reversal = False

    # MEASURED lateral acceleration. `curvature` is controlsState.curvature —
    # the vehicle model's reading of the STEERING ANGLE, which exists whether or
    # not openpilot is steering. It is NOT modelV2.orientationRate, which is the
    # model's PLAN and reports what it intends rather than what happened (the
    # v3.4.9 / v3.5.4 trap). `carState.yawRate` would be the obvious source and
    # is a silent zero on this platform: only PSA and Ford populate it in
    # opendbc, Hyundai never assigns it.
    v = float(v_ego) if _finite(v_ego) else 0.0
    k = float(curvature) if _finite(curvature) else 0.0
    self.a_lat = v * v * abs(k)

    self.limited = bool(lat_active) and (
      bool(saturated) or bool(eps_limited)
      or (_finite(steer_torque) and abs(float(steer_torque)) >= TBAR_LIMIT))

    if not _finite(steering_angle_deg) or not _finite(dt) or dt <= 0.0:
      return
    ang = float(steering_angle_deg)
    if self._ema is None:
      self._ema = ang
      return
    a = 1.0 - math.exp(-dt / OSC_TAU_S) if OSC_TAU_S > 0 else 1.0
    self._ema += (ang - self._ema) * a
    hp = ang - self._ema

    s = 1 if hp > 0 else (-1 if hp < 0 else 0)
    if s == 0:
      return
    if self._sign == 0:
      self._sign = s
      self._peak = abs(hp)
      return
    if s == self._sign:
      self._peak = max(self._peak, abs(hp))
      return
    # sign changed: it is a reversal only if the excursion that preceded it was
    # big enough to be a correction rather than noise around zero.
    if self._peak >= OSC_AMP_DEG:
      self.reversal = True
    self._sign = s
    self._peak = abs(hp)


class CornerPass:
  """One traversal of one corner, accumulated.

  `verdict()` is the whole output: the peak lateral acceleration the car pulled,
  and how far over the limit the pass was.
  """

  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self.open = False
    self.duration = 0.0
    self.a_peak = 0.0
    self.reversals = 0
    self.limit_time = 0.0
    self.v_min = 1e9
    self.blocked = False       # something happened that makes this pass unusable

  def begin(self) -> None:
    self.reset()
    self.open = True

  def add(self, effort: LateralEffort, dt: float, v_ego: float,
          blocked: bool = False) -> None:
    if not self.open or not _finite(dt) or dt <= 0.0:
      return
    self.duration += dt
    self.a_peak = max(self.a_peak, effort.a_lat)
    self.v_min = min(self.v_min, float(v_ego) if _finite(v_ego) else 0.0)
    self.reversals += int(effort.reversal)
    if effort.limited:
      self.limit_time += dt
    self.blocked = self.blocked or bool(blocked)

  def usable(self) -> bool:
    return (self.open and not self.blocked and self.duration >= MIN_PASS_S
            and self.v_min >= MIN_PASS_V and self.a_peak > 0.0)

  def verdict(self) -> tuple[float, float]:
    """(a_peak, severity). severity 1.0 means exactly at the limit.

    The two components are compared, not summed: either one being over is
    enough to condemn the pass, and adding them would let two half-breaches
    manufacture a full one.
    """
    if self.duration <= 0.0:
      return 0.0, 0.0
    osc_rate = self.reversals / self.duration
    limit_frac = self.limit_time / self.duration
    sev = max(osc_rate / OSC_RATE_LIMIT, limit_frac / LIMIT_FRAC_LIMIT)
    return self.a_peak, min(sev, MAX_SEVERITY)
