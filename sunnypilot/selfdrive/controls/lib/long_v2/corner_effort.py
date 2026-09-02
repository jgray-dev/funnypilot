"""FunnyPilot v3.6.2 — how hard did the car have to work to get round that?

ONLY PASSES OPENPILOT ITSELF DROVE ARE MEASURED (v3.6.4, MIN_ENGAGED_FRAC).
A tired or distracted driver drifting wide is evidence about the driver, and
filing it against the corner makes that bend permanently slower for a reason
that has nothing to do with the road.

SCC-M v2's definition of the ideal corner speed is the one that was asked for:
AS FAST AS POSSIBLE WITHOUT lateral oscillation, without the driver-torque
clamp cutting our steering request, without the steering controller hitting
its limits, WITHOUT LEAVING THE LANE (v3.6.4) — and, since v3.6.5, WITHOUT THE
DRIVER FEELING THE NEED TO TAKE OVER. This module turns those five into a
number, per pass, so the answer is measured rather than inferred from how the
car "felt".

────────────────────────────────────────────────────────────────────────────
THE FIVE SIGNALS, AND WHERE EACH ONE COMES FROM

  oscillation     `steeringAngleDeg`, high-passed. The steady ramp of steering
                  into a bend is removed; what is left is correction. Counting
                  amplitude-qualified sign changes gives reversals per second,
                  which is a rate the driver would describe as "sawing".
                  A driver fighting a corner looks identical to a controller
                  fighting one, which is exactly why a pass the driver steered
                  is not evidence — see MIN_ENGAGED_FRAC.

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

  lane departure  `modelV2.laneLines` at the car. THE MOST DIRECT OF THE FIRST
                  FOUR: the other three ask how hard the controller worked,
                  this one asks whether the car stayed where it belonged, which
                  is what "too fast for the bend" means physically.

  takeover        (v3.6.5) openpilot was driving the bend and the human took an
                  axis back — lateral off, longitudinal off, or the brake
                  pedal. The only signal with a person in it, and it is not a
                  measurement of the person: it is their verdict on OUR speed.
                  See TAKEOVER_SEVERITY for why that does not reopen what
                  v3.6.4 closed.

────────────────────────────────────────────────────────────────────────────
AND ONE THING THAT MUST BE SUBTRACTED (v3.6.2)

The controller-effort signals above assume that if the car is working hard,
the SPEED is why. On this car that is demonstrably not always true: v3.3.8 recorded sawing
at a railroad crossing with the EPS governor at full authority and nothing
wrong with the speed. A bump unloads the front axle, the self-aligning torque
changes, and the controller corrects for the road rather than for the corner.
Without a guard, one crossing inside a bend teaches the store that a perfectly
good corner is slow, permanently and invisibly.

So the pass carries `clean_duration` alongside `duration`, and the severity
rates are taken over the clean part only. The disturbed samples leave BOTH the
numerator and the denominator — see CornerPass.add for why removing them from
only one side is worse than not guarding at all.

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

# ── who was steering ───────────────────────────────────────────────────────
#
# v3.6.4 — ONLY OPENPILOT'S OWN PASSES ARE EVIDENCE. THIS REVERSES REQUIREMENT
# 4 OF THE ORIGINAL BRIEF ON PURPOSE; do not "restore" it without reading this.
#
# The brief asked for learning regardless of engagement, on the reasoning that
# a bend driven far too fast by hand shows how far past the limits it was. The
# owner's counter-example is decisive and it applies to the whole measure, not
# to one signal: drift wide through a bend because you are tired, distracted,
# or looking at the wrong thing, and every signal that is still live with the
# driver steering reports it as "this corner is too fast" — so the store files
# a permanently slower corner on evidence about the HUMAN.
#
# It is not a partial problem. `limited` (the torque clamp, saturation, the EPS
# governor) is ALREADY gated on lat_active, because with the driver steering a
# high torque reading is just the driver driving. So with lateral off, severity
# is composed ENTIRELY of oscillation and lane departure — the two signals that
# measure the person rather than the road. There is nothing left that is about
# the corner.
#
# The asymmetry seals it. A driver-caused reading almost always LOWERS the
# ceiling, which is the direction that sticks and the direction with no
# symptom: a corner that is too slow produces no complaint, no alert and no
# oscillation, so nothing ever revisits it. Learning would drift quietly
# downward with the driver's worst days as its evidence.
#
# THE COST, STATED: the store now only fills on engaged drives, so it fills
# more slowly. That is the right trade — a slower-filling map of measurements
# the car actually made beats a fast-filling map of measurements about the
# driver.
MIN_ENGAGED_FRAC = 0.95   # of the pass, with openpilot steering

# ── the driver's own verdict (v3.6.5) ──────────────────────────────────────
#
# A FIFTH SIGNAL, AND THE ONLY ONE WITH A HUMAN IN IT. THIS DOES NOT REOPEN
# WHAT v3.6.4 CLOSED, and the distinction is the whole design:
#
#   v3.6.4 threw out passes THE DRIVER DROVE, because the signals then measure
#   how well the human steered and file it against the road.
#   v3.6.5 keeps passes OPENPILOT DROVE and the human INTERRUPTED. That is not
#   a measurement of the human at all — it is the human's judgement of
#   openpilot's speed, and it is the most direct statement of "too fast for
#   this bend" available anywhere in the car.
#
# So a takeover is only evidence on a pass that STARTED engaged, and the
# measurement ENDS at the takeover: everything after it is the human driving,
# which is exactly what v3.6.4 rules out.
#
# WHAT COUNTS. Lateral disengagement, longitudinal disengagement, and the brake
# pedal. NOT the accelerator — a driver adding throttle mid-bend is evidence we
# were too SLOW, and folding it in here would let the one signal that argues
# for more speed lower the ceiling instead.
#
# v3.6.5 — AND EXCLUDING IT TAKES AN EXPLICIT `gas_pressed`, WHICH v3.6.5 DID
# NOT HAVE. `controlsd.py:139` clears `CC.longActive` for ANY event carrying
# `overrideLongitudinal`, and `pedalPressed` / `gasPressedOverride` are exactly
# that — so on this stack a gas press LOOKS IDENTICAL to the driver switching
# longitudinal off. v3.6.5 therefore scored every mid-corner throttle input as
# a takeover at severity 2.0: the one input that argues the corner is too SLOW
# was lowering its ceiling. `long_active` is now only believed when the pedal
# is up.
#
# 2.0 means "this corner supports about half the lateral acceleration we just
# pulled". Stronger than being at the limit (1.0), because a person physically
# intervened; short of MAX_SEVERITY (3.0), because a takeover says the speed
# was wrong without saying by how much, and a single pass may not condemn a
# corner outright. It enters through the same max() as the other four, so a
# pass that was ALSO sawing reports whichever verdict is worse.
TAKEOVER_SEVERITY = 2.0
# A takeover is a discrete EVENT, not a rate, so it does not need MIN_PASS_S of
# window to mean something — and requiring one would discard exactly the case
# that matters most, a driver grabbing the wheel in the first half second
# because the car entered far too fast. The rate-based signals still need their
# window and simply contribute nothing when they do not have it.
MIN_TAKEOVER_S = 0.3
# v3.7.0 — A TAKEOVER AT JUNCTION-GRADE CURVATURE IS A TURN-OFF, NOT A VERDICT.
# TAKEOVER_SEVERITY was designed for "openpilot entered the bend too fast and I
# grabbed it". A driver turning into a driveway or side road also takes the
# wheel — and steers far tighter than any road bend: 0.04 1/m is a 25 m radius,
# which is a junction, not a corner a car cruises through. Above this the
# takeover is the driver LEAVING the road, and the pass says nothing about the
# bend it happened inside. Measured from the effort's own a_lat / v^2 on the
# override frames, so it needs no new signal.
JUNCTION_K = 0.04
# How long the human has to actually hold an axis before it reads as a
# takeover. `latActive` drops for a frame at plenty of boundaries that are not
# interventions — a model drop, the moment a corner leaves the list, the edge
# of an engage transition — and a single frame must not be able to condemn a
# corner. Short enough that a real grab is caught well before the apex.
TAKEOVER_DWELL_S = 0.25

# ── the driver's demonstration (v3.6.5) ────────────────────────────────────
#
# THE MIRROR OF THE TAKEOVER, AND THE ONLY SIGNAL IN THIS FILE THAT CAN MAKE A
# CORNER FASTER ON ONE PASS. Owner-requested, and the argument is airtight in a
# way none of the others are:
#
#   openpilot is STEERING the bend. Nothing is oscillating, nothing is clamped,
#   nothing is saturated, the car is inside its lane. The driver holds the
#   accelerator. Whatever lateral acceleration the car reaches under those
#   conditions is not an estimate of what the corner supports — it is a
#   DEMONSTRATION that it supports it, made by the two parties who would know.
#
# So a demonstrated pass adopts `a_peak` outright (`seed`) instead of easing
# toward it at ALPHA_FLOOR, which is what "learn quicker" has to mean when the
# quantity is a floor: not a bigger step, but no discount on evidence that is
# not actually uncertain.
#
# EVERY GUARD STAYS ON, AND THEY ARE WHAT MAKE THIS SAFE. The pass must be
# CLEAN (severity below CLEAN_TH — the same bar any floor-raising pass has to
# clear), openpilot must have steered essentially all of it (MIN_ENGAGED_FRAC),
# the road must not have been hitting us (MIN_CLEAN_FRAC), and A_LAT_MAX still
# bounds the result. What is removed is the EMA discount, not a safety check.
MIN_DEMO_S = 0.5      # of held throttle with lateral active; one tap is not a demonstration

# ── running out of lane ────────────────────────────────────────────────────
#
# v3.6.4 — THE FOURTH SIGNAL, AND THE MOST DIRECT ONE. The other three ask how
# hard the CONTROLLER worked; this one asks whether the car actually stayed
# where it was supposed to be. Leaving the lane through a bend is not a proxy
# for "too fast", it is the thing itself, and it is exactly what the driver
# notices on a blind corner that turns out tighter than it looked.
#
# GEOMETRY — AND v3.6.5 GOT THE SIGN OF IT WRONG, WHICH IS WHY THE DEV UI READ
# A CONSTANT 0 cm FOR A WHOLE RELEASE. openpilot's device frame is
# **x forward, y POSITIVE RIGHT, z down** (common/transformations/camera.py:73),
# not y-positive-left. `ldw.py` confirms it independently: it tests the LEFT
# line against a NEGATIVE bound (`laneLines[1].y[0] > -(1.08 + CAMERA_OFFSET)`)
# and the RIGHT against a positive one. So at the car the left line sits at
# y ~ -1.85 and the right at y ~ +1.85, and the ego lane's width is
# `y_right - y_left`.
#
# THE WAY IT FAILED IS THE INTERESTING PART. With the signs swapped the width
# came out at -3.7 m, which fails the LANE_W_MIN_M..LANE_W_MAX_M plausibility
# gate on every single frame, so the function returned 0.0 always. Had that
# gate not been there, `max(0, HALF_TRACK - y_left, HALF_TRACK + y_right)` on
# a perfectly centred car would have reported 2.78 m of departure — severity
# pinned at MAX on every pass, and every corner in the store learned as far too
# fast. The sanity gate turned a dangerous wrong answer into a visibly dead
# one, which is the whole reason to write gates that fail toward "measured
# nothing".
#
# Corrected, the car's own edges are at +/- HALF_TRACK_M and how far a wheel is
# PAST a line is
#
#     max(0, y_left + HALF_TRACK_M, HALF_TRACK_M - y_right)
#
# which is zero while both edges are inside the lines and grows in metres once
# one is not. Centred in a 3.7 m lane that evaluates to exactly 0.
#
# 1.86 m is the K5's body width; half of it is the distance from the centreline
# the model measures against to the outside of a tyre. Mirrors are excluded on
# purpose — a mirror overhanging a line is not a lane departure.
HALF_TRACK_M = 0.93
# The camera is not on the car's centreline, and `laneLines.y` is in the
# DEVICE frame, so the car's centreline sits at y = -CAMERA_OFFSET. ldw.py
# carries the same asymmetry (`-(1.08 + CAMERA_OFFSET)` on the left,
# `(1.08 - CAMERA_OFFSET)` on the right) and it is 4 cm — the same order as the
# false readings v3.6.5 produced, so it is not worth leaving out.
CAMERA_OFFSET_M = 0.04
# Below this the model is guessing where the line is, and a guessed line is a
# reason to measure NOTHING rather than to measure something wrong.
LANE_PROB_MIN = 0.5
# v3.6.5 — THE MODEL EXTRAPOLATES THE LINES AT x = 0 AND THAT IS THE NOISIEST
# POINT ON THE POLYLINE. `laneLines[i].y[0]` is beside the car, which the
# forward camera cannot see; the model infers it. Reported symptom: 5-6 cm of
# "departure" with the car dead centre in its lane. Anything under this is
# the extrapolation, not a wheel over a line.
DEPART_DEADBAND_M = 0.10
# ...and a PEAK over a four-second pass of a noisy signal is the noise floor,
# not the signal, so the departure is low-passed before it is peaked. v3.6.4
# argued "it is a peak, not a rate — one wheel over the line does not have to
# persist to count", which is right about the ROAD and wrong about the SENSOR:
# a single frame of a guessed line is not a wheel anywhere. 0.20 s still counts
# a genuine clip of a line and rejects a one-frame excursion outright.
DEPART_TAU_S = 0.20
# Sanity on the lane the model reports. Outside this it has probably latched a
# road edge, a kerb or the far side of a junction, and the departure computed
# from it would be fiction.
LANE_W_MIN_M = 2.3
LANE_W_MAX_M = 4.6
# Departure at which the pass is exactly at the limit. A quarter of a metre
# past the line is unambiguous — well beyond the model's own lateral noise,
# and visibly outside the lane from the driver's seat.
DEPART_LIMIT_M = 0.25
# A single pass may not claim more than this, for the same reason MAX_SEVERITY
# exists: one swerve is not a measurement of the corner.
MAX_DEPART_M = 1.0

# ── road disturbance ───────────────────────────────────────────────────────
#
# v3.6.2 — THE CONFOUND THIS CAR IS KNOWN TO HAVE. The whole severity measure
# assumes that oscillation means "too fast for this bend". On this car it does
# not always: the v3.3.8 investigation recorded sawing at a railroad crossing
# with the EPS governor pinned at 100% authority and nothing wrong with the
# speed — the front axle unloads, the self-aligning torque changes, and the
# controller corrects for a disturbance rather than for the corner. Left
# unguarded, one bumpy crossing mid-bend teaches the store that a perfectly
# good corner must be taken slowly, permanently.
#
# `pitch_rate_deg_s` is the peak |car-frame Y angular rate| controlsd already
# publishes as field 2 of /dev/shm/lat_interp. It costs no new signal and no
# new channel; it was simply never read here.
#
# 5.0 deg/s is bump_damper.TRIGGER_DEG_S, deliberately the SAME number: both
# answer "is the road hitting the car right now", and two thresholds for one
# question drift. Measured in v3.3.8: baseline under 3, confirmed event peak 7.
DISTURB_DEG_S = 5.0
# How long after the last supra-threshold frame the pass stays contaminated.
# NOT cosmetic and not the same as the bump itself: the v3.3.8 data showed the
# oscillation STARTING AFTER the pitch rate had decayed, because the lateral
# delay buffer replays the corrupted measurement as a corrupted SETPOINT one
# lat_delay later (~0.5 s), and the car then rings for a beat. 1.2 s covers
# the replay and the settle; bump_damper's own HOLD_S + RECOVER_S is 1.5 s.
DISTURB_HOLD_S = 1.2
# A pass has to retain this much undisturbed time to be measured at all.
# Below it the rate statistics are being computed over a sliver and mean
# nothing — which is a reason to discard the pass, not to trust it.
MIN_CLEAN_FRAC = 0.5


def _finite(x) -> bool:
  try:
    f = float(x)
  except (TypeError, ValueError):
    return False
  return f == f and abs(f) != float('inf')


def lane_departure_m(y_left: float, y_right: float,
                     p_left: float, p_right: float) -> float:
  """How far the car's nearer edge is OUTSIDE the ego lane, metres. v3.6.4.

  `y_*` are the model's lane lines at the car, in openpilot's device frame,
  which is **+y to the RIGHT** — so `y_left` is normally NEGATIVE and
  `y_right` positive. v3.6.5 corrected this; see the block above HALF_TRACK_M
  for how the previous sign convention failed and why it failed silently.

  `p_*` are the matching `laneLineProbs`. Returns 0.0 for "inside the lane"
  AND for "cannot tell", and those are deliberately the same answer: an
  unreadable lane must contribute no stress, so a model that has lost the
  lines can only ever make a pass look cleaner than it was, never worse. The
  other three signals still cover the pass.

  Pure and stdlib-only so the geometry can be tested without a model running;
  the capnp unpacking lives in the planner.
  """
  if not (_finite(y_left) and _finite(y_right) and _finite(p_left) and _finite(p_right)):
    return 0.0
  if p_left < LANE_PROB_MIN or p_right < LANE_PROB_MIN:
    return 0.0
  width = float(y_right) - float(y_left)
  if not (LANE_W_MIN_M <= width <= LANE_W_MAX_M):
    return 0.0
  # The car's edges in the DEVICE frame: centreline at -CAMERA_OFFSET_M.
  left_edge = -CAMERA_OFFSET_M - HALF_TRACK_M
  right_edge = -CAMERA_OFFSET_M + HALF_TRACK_M
  out = max(0.0, float(y_left) - left_edge, right_edge - float(y_right))
  # v3.6.5 deadband: under this it is the model's x=0 extrapolation, not a
  # wheel. Subtracted rather than thresholded so the signal stays continuous —
  # a step at the deadband would make DEPART_LIMIT_M mean something different
  # either side of it.
  out = max(0.0, out - DEPART_DEADBAND_M)
  return min(out, MAX_DEPART_M)


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
    self._disturb_hold = 0.0
    self._dep_lp = 0.0         # low-passed lane departure; see DEPART_TAU_S
    self.reversal = False      # a qualified steering reversal happened this frame
    self.a_lat = 0.0           # measured lateral acceleration, m/s^2
    self.limited = False       # a hard lateral limit is being hit this frame
    self.disturbed = False     # the road is hitting the car; see DISTURB_DEG_S
    self.departure = 0.0       # metres our nearer edge is outside the lane
    self.engaged = False       # openpilot was steering this frame
    self.override = False      # the human has taken the WHEEL this frame
    self.long_manual = False   # ...or is choosing the speed by pedal
    self.demo = False          # ...which, with lateral still on, is a DEMONSTRATION

  def update(self, dt: float, v_ego: float, curvature: float, steering_angle_deg: float,
             steer_torque: float, lat_active: bool, saturated: bool = False,
             eps_limited: bool = False, pitch_rate_deg_s: float = 0.0,
             departure_m: float = 0.0, lane_change: bool = False,
             long_active: bool = True, brake_pressed: bool = False,
             gas_pressed: bool = False) -> None:
    self.reversal = False
    self.engaged = bool(lat_active)

    # v3.6.5 — IS A HUMAN IN CHARGE OF ANY AXIS RIGHT NOW? Three facts, no
    # inference: lateral handed back, longitudinal handed back, or the brake
    # pedal down.
    #
    # v3.6.5 — `long_active` IS ONLY BELIEVED WITH THE ACCELERATOR UP, because
    # on this stack a gas press CLEARS it (controlsd raises `pedalPressed`,
    # which carries `overrideLongitudinal`). Without that term the one input
    # arguing the corner is too slow scored as a takeover at severity 2.0.
    # `gas_pressed` defaults False so a caller that does not know about the
    # pedal gets the v3.6.5 answer rather than a silently different one.
    gas = bool(gas_pressed)

    # v3.6.6 — LONGITUDINAL AND LATERAL HANDOVERS ARE NO LONGER THE SAME EVENT,
    # and the split is the owner's rule: "if we take manual control of long
    # going through a corner, the speed we take it at should only ever RAISE the
    # corner's speed."
    #
    # The reasoning holds up. A driver choosing to go SLOWER through a bend is
    # not evidence about the bend — they may be behind traffic, unsure of the
    # road, or simply cautious — so a 45 mph corner taken by hand at 35 must
    # stay a 45 mph corner. A driver going FASTER while openpilot still holds
    # the line IS evidence, and the strongest available. Neither of those is a
    # reason to lower the ceiling, so a longitudinal handover makes the pass
    # RAISE-ONLY rather than condemning it.
    #
    # `override` — the thing that TRUNCATES the pass and carries
    # TAKEOVER_SEVERITY — is therefore LATERAL ONLY from v3.6.6. That is also
    # what makes the raise measurable at all: with lateral still active the
    # oscillation, clamp, saturation and lane signals are still OURS, so the
    # pass can be judged clean and its `a_peak` believed.
    self.override = not bool(lat_active)
    # ...and this is "the human owns the speed right now", by any route.
    self.long_manual = gas or bool(brake_pressed) or (not bool(long_active))

    # THE DEMONSTRATION. Only while openpilot still has the wheel: with the
    # driver steering, the speed they choose says nothing about what the
    # CONTROLLER can do through the bend, which is the whole v3.6.4 argument.
    self.demo = self.long_manual and bool(lat_active)

    # A LANE CHANGE IS NOT A LANE DEPARTURE. Crossing a line on purpose says
    # nothing about the corner, so the signal is suppressed outright rather
    # than merely damped — this is the one case where the measurement is not
    # noisy, it is about something else entirely.
    raw_dep = 0.0 if lane_change else (
      float(departure_m) if (_finite(departure_m) and departure_m > 0.0) else 0.0)
    # ...and v3.6.5 low-passes what is left, because the geometry it comes from
    # is an extrapolation at x = 0. See DEPART_TAU_S. A lane change resets the
    # filter rather than decaying through it, or the tail of a deliberate line
    # crossing would land on the corner that follows it.
    if lane_change:
      self._dep_lp = 0.0
    else:
      step = float(dt) if (_finite(dt) and dt > 0.0) else 0.0
      a = 1.0 - math.exp(-step / DEPART_TAU_S) if step > 0.0 else 0.0
      self._dep_lp += (raw_dep - self._dep_lp) * a
    self.departure = max(0.0, self._dep_lp)

    # Road disturbance. Held past the event because the correction it provokes
    # ARRIVES LATE — see DISTURB_HOLD_S. Failure defaults to NOT disturbed, so
    # an unreadable pitch signal degrades to exactly the pre-v3.6.2 behaviour
    # rather than silently suppressing every measurement the store lives on.
    pr = abs(float(pitch_rate_deg_s)) if _finite(pitch_rate_deg_s) else 0.0
    step = float(dt) if _finite(dt) and dt > 0.0 else 0.0
    if pr >= DISTURB_DEG_S:
      self._disturb_hold = DISTURB_HOLD_S
    else:
      self._disturb_hold = max(0.0, self._disturb_hold - step)
    self.disturbed = self._disturb_hold > 0.0

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
    self.clean_duration = 0.0  # duration minus time the road was hitting us
    self.engaged_duration = 0.0  # time openpilot was steering; see MIN_ENGAGED_FRAC
    self.a_peak = 0.0
    self.depart_peak = 0.0     # worst lane departure, metres
    self.reversals = 0
    self.limit_time = 0.0
    self.v_min = 1e9
    self.blocked = False       # something happened that makes this pass unusable
    self.engaged_at_start = False  # openpilot had both axes when the bend began
    self.took_over = False     # ...and the human then intervened; see TAKEOVER_SEVERITY
    self._started = False
    self._ended = False        # the human is driving now; stop measuring
    self._override_t = 0.0     # how long they have held the wheel; TAKEOVER_DWELL_S
    self.takeover_k = 0.0      # v3.7.0: tightest curvature while they held it; see JUNCTION_K
    self.demo_time = 0.0       # driver chose the speed, lateral on; MIN_DEMO_S
    self.long_manual = False   # the driver owned the speed at some point

  def begin(self) -> None:
    self.reset()
    self.open = True

  def add(self, effort: LateralEffort, dt: float, v_ego: float,
          blocked: bool = False, lead: bool = False) -> None:
    if not self.open or not _finite(dt) or dt <= 0.0:
      return

    # v3.6.5 — THE MEASUREMENT ENDS AT THE TAKEOVER. Whatever happens after a
    # driver intervenes is the driver driving, which is precisely what
    # MIN_ENGAGED_FRAC exists to keep out of the store. Latching here rather
    # than filtering later is also what keeps `engaged_fraction` honest: the
    # pass IS the engaged part, so a takeover cannot make its own pass fail the
    # engagement gate it just triggered.
    #
    # A LEAD DISARMS THE VERDICT WITHOUT DISARMING THE TRUNCATION, and this is
    # the ONE exclusion the v3.6.2 design deliberately did not need. That
    # argument was "the interval cannot be poisoned by something that did not
    # happen laterally", and a lead produces no lateral acceleration — true of
    # the other four signals, and NOT true of this one, because a takeover is a
    # discrete event rather than a measurement of the bend. Braking for a car
    # that slowed in front of us mid-corner would otherwise condemn the corner,
    # and on a first visit `seed=True` would adopt that outright. So with a lead
    # present the pass still ENDS here — the human is driving and none of what
    # follows is ours — but it does not carry a verdict.
    if self._ended:
      return
    if not self._started:
      self._started = True
      self.engaged_at_start = bool(effort.engaged) and not bool(effort.override)

    # THE SKIP IS CONDITIONAL ON THE PASS HAVING BEEN OURS, and that is not
    # tidiness. Frames where the human has an axis are not a measurement of the
    # road, so once a pass is ours they are dropped whole — which is also what
    # keeps `engaged_fraction` from being dragged under MIN_ENGAGED_FRAC by the
    # dwell window itself. But a pass that was NEVER ours must keep
    # accumulating, or it would be rejected for being empty and MIN_ENGAGED_FRAC
    # would stop being the thing that rejects it — a guard nothing can reach is
    # not a guard.
    if self.engaged_at_start:
      if effort.override:
        self._override_t += dt
        # v3.7.0 — remember HOW the human steered while they had it. The
        # effort's a_lat is v^2*|k|, so this recovers |k| without a new input;
        # `_commit_pass` reads it against JUNCTION_K to tell a turn-off from a
        # correction. Kept on the override frames only, because that is the
        # steering that is the human's.
        v2 = float(v_ego) * float(v_ego) if _finite(v_ego) else 0.0
        if v2 > 1.0 and _finite(effort.a_lat):
          self.takeover_k = max(self.takeover_k, float(effort.a_lat) / v2)
        if self._override_t >= TAKEOVER_DWELL_S:
          self._ended = True
          self.took_over = not bool(lead)
        return
      self._override_t = 0.0

    self.duration += dt
    self.a_peak = max(self.a_peak, effort.a_lat)
    self.v_min = min(self.v_min, float(v_ego) if _finite(v_ego) else 0.0)
    self.blocked = self.blocked or bool(blocked)
    # Counted over the WHOLE pass, not the clean part: who was in control is a
    # separate question from whether the road was hitting us.
    if effort.engaged:
      self.engaged_duration += dt

    # v3.6.2 — DISTURBED TIME IS EXCISED FROM BOTH SIDES OF THE RATE, and
    # doing only one is the trap. Dropping the reversals but keeping the time
    # makes a bumpy pass look CLEANER than it was, which can raise the floor
    # and buy speed off a measurement that was never taken — the dangerous
    # direction. Dropping the time but keeping the reversals makes it look
    # WORSE, which is the original bug. Removing the contaminated samples from
    # numerator AND denominator leaves an honest rate over the part of the
    # bend where nothing was hitting the car, and biases neither way.
    #
    # `a_peak` and `v_min` DELIBERATELY still accumulate: how fast we actually
    # went round the bend is a fact the road surface does not change.
    if effort.disturbed:
      return
    self.clean_duration += dt
    self.reversals += int(effort.reversal)
    if effort.demo:
      self.demo_time += dt
    if effort.long_manual:
      self.long_manual = True
    if effort.departure > self.depart_peak:
      self.depart_peak = min(effort.departure, MAX_DEPART_M)
    if effort.limited:
      self.limit_time += dt

  @property
  def turned_off(self) -> bool:
    """v3.7.0 — the takeover was the driver leaving the road, not correcting
    the bend. Discards the pass instead of condemning the corner."""
    return bool(self.took_over) and self.takeover_k >= JUNCTION_K

  def engaged_fraction(self) -> float:
    """How much of the pass openpilot steered. 1.0 = all of it."""
    if self.duration <= 0.0:
      return 0.0
    return self.engaged_duration / self.duration

  def demonstrated(self) -> bool:
    """The driver chose the speed through a bend openpilot steered cleanly.

    v3.6.5 — see MIN_DEMO_S. The CLEAN_TH term is what keeps this from being a
    licence: a pass that oscillated or left the lane is not a demonstration of
    anything however much throttle was applied, and it takes the ceiling down
    through `verdict()` as usual.
    """
    return (self.demo_time >= MIN_DEMO_S and not self.took_over
            and self.verdict()[1] < CLEAN_TH)

  def clean_fraction(self) -> float:
    """How much of the pass was measurable. 1.0 = nothing hit the car."""
    if self.duration <= 0.0:
      return 0.0
    return self.clean_duration / self.duration

  def usable(self) -> bool:
    """The clean part has to be long enough on its own. A four-second bend of
    which three seconds were a level crossing is not a four-second
    measurement — the rate statistics would be computed over the remaining
    sliver and would mean nothing.

    v3.6.5 — A TAKEOVER IS EXEMPT FROM THE RATE WINDOWS AND ONLY FROM THOSE.
    It is an event, not a rate, so MIN_PASS_S / MIN_CLEAN_FRAC do not apply to
    it; every other gate (engaged at the start, openpilot steering up to the
    intervention, fast enough to be informative, a real corner behind it) still
    does. `verdict()` correspondingly drops the rate terms when there is no
    window to compute them over, so an exempted pass reports the takeover and
    nothing invented.
    """
    if not self.open or self.blocked or self.v_min < MIN_PASS_V or self.a_peak <= 0.0:
      return False
    if not self.engaged_at_start or self.engaged_fraction() < MIN_ENGAGED_FRAC:
      return False
    if self.took_over:
      return self.duration >= MIN_TAKEOVER_S
    return (self.duration >= MIN_PASS_S and self.clean_duration >= MIN_PASS_S
            and self.clean_fraction() >= MIN_CLEAN_FRAC)

  def verdict(self) -> tuple[float, float]:
    """(a_peak, severity). severity 1.0 means exactly at the limit.

    The components are compared, not summed: any one of them being over is
    enough to condemn the pass, and adding them would let two half-breaches
    manufacture a full one.

    Rates are over `clean_duration`, not `duration` — see add() — and are
    DROPPED ENTIRELY below MIN_PASS_S of clean window. That is not the same as
    computing them anyway: one reversal in a fifth of a second is a rate of
    5/s, three times the limit, off a sample far too short to mean it. Only the
    takeover path can reach here with such a window, and it carries its own
    verdict.
    """
    sev = 0.0
    if self.clean_duration >= MIN_PASS_S:
      sev = max(self.reversals / self.clean_duration / OSC_RATE_LIMIT,
                (self.limit_time / self.clean_duration) / LIMIT_FRAC_LIMIT)
    # v3.6.4: leaving the lane is a PEAK rather than a rate — one wheel a
    # quarter of a metre outside the line is a fact about the corner, not
    # something that has to persist to count.
    sev = max(sev, self.depart_peak / DEPART_LIMIT_M)
    # v3.6.5: and so is a human deciding to intervene.
    if self.took_over:
      sev = max(sev, TAKEOVER_SEVERITY)
    return self.a_peak, min(sev, MAX_SEVERITY)
