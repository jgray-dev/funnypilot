"""FunnyPilot v3.4.9 — KnotFilter: spend the lagd window on what the plan did NOT predict.

WHY THIS EXISTS. lat_smooth.py already spreads each 20 Hz model action across
the five 100 Hz control frames that follow it, so the wheel provably moves
every frame. What it CANNOT do is make the knot SEQUENCE itself smoother: the
knots are the model's raw actions, and a rate change between two of them is
delivered whole inside one 50 ms period. That step is what is felt in the car
as a "sharp adjustment", and it is the thing this module reduces.

THE FAILURE WE ARE NOT REPEATING (v3.2.12, reverted in v3.3.2). The previous
attempt to spend the lagd delay window on smoothness was an EMA on the action.
An EMA's implicit process model is "the curvature stays where it is", so it
lags EVERYTHING — including a steady, entirely predictable turn-in — by its
time constant, without bound and without any way to say how far the command
had drifted from the model's desire. On the road that is exactly the reported
"sloppy / the car is somewhere between the two places the model wanted".

THE DIFFERENCE. This filter's process model is the MODEL'S OWN PUBLISHED PLAN.
controlsd already samples the plan one model step past the action horizon
(`_model_lookahead_curv`, used since v3.3.6 to aim the in-period spline's exit
slope). That sample is, by construction, a PREDICTION of the next frame's
action: the action is the plan at `lat_delay + DT_MDL`, and the lookahead is
the same plan at `lat_delay + 2*DT_MDL`, i.e. the same absolute instant the
next action will describe. So each model frame we can ask a question an EMA
never could:

    innovation  n = raw_action - (what the previous plan said this would be)

and damp ONLY n. Content the plan predicted — a constant-rate turn-in, holding
a steady curve, unwinding on a known schedule — has n == 0 and passes through
COMPLETELY UNCHANGED, at its validated time. There is no phase lag on
predicted motion, which is the whole reason the EMA felt sloppy and this does
not.

WHAT IT DAMPS, AND BY HOW MUCH. The output is

    out_k = raw_k + offset_k
    offset_k = (1 - beta) * (CARRY * offset_{k-1} - n_k)          [1]

  * beta is INNOVATION-ADAPTIVE. A small surprise is mostly model noise, so it
    is damped hard (beta -> BETA_MIN); a large surprise is either a genuine
    maneuver onset or an evasive command, so it passes essentially untouched
    (beta -> 1). Decisive onsets stay decisive — the v3.3.2 post-mortem's
    binding requirement ("no partial steering in the space before the 5").
  * CARRY < 1 spreads a damped surprise over several model frames instead of
    dumping the remainder on the next one. With BETA_MIN = 0.30 the geometric
    factor is at most (1-0.30)*0.6 = 0.42 per frame, so [1] is a CONTRACTION:
    once the innovations stop, the offset decays to zero with a ~58 ms time
    constant. It cannot accumulate and it cannot latch.
  * DEV_MAX_LAT_ACCEL is a HARD CAP on the whole point of concern. The command
    may never differ from the model's desire by more than 0.15 m/s^2 of
    lateral acceleration. That bound is what makes "the car ends up somewhere
    the model did not want" quantifiable instead of a feeling: holding the
    full cap for a whole 200 ms convergence displaces the path by
    0.5 * 0.15 * 0.2^2 = 3 mm. In practice the reachable deviation is smaller
    still (~0.10 m/s^2 on a sustained maneuver at the model's own rate rail,
    i.e. under one model frame of lag) because beta rises with the innovation
    — the cap is a backstop for a WRONG prediction, not the operating point.

Everything downstream is unchanged: the filtered value is a KNOT, handed to
LatSmoother exactly as the raw action was, reached on the same validated
schedule, and clip_curvature still enforces the ISO jerk/accel limits.

Import-light (stdlib only).
"""
import math

# Damping floor: fraction of a fully-unpredicted change passed through on the
# frame it arrives. 0.30 leaves 70% to be spread over the following frames.
BETA_MIN = 0.30

# Innovation magnitude (as lateral acceleration, m/s^2) at which damping is
# fully released. Sized ABOVE the model's own per-frame action-rate ceiling
# (drive_helpers.MAX_TARGET_LAT_JERK * DT_MDL = 0.125 m/s^2), deliberately: a
# change AT that rail is the sharpest adjustment the model can normally ask for
# and is exactly the one that is felt in the car, so it gets damped (beta ~=
# 0.45, i.e. a little over half of it spread across the following frames).
# Full release is reserved for innovations several times larger than the model
# can produce through its own limiter — a replan the plan genuinely did not
# see coming, where lag is the wrong trade.
N_FULL_LAT_ACCEL = 0.6

# Fraction of the outstanding deviation carried into the next frame. This is
# what turns "delay a step by one frame" into "spread it over several", and it
# is bounded well below 1/(1-BETA_MIN) so [1] is always a contraction.
CARRY = 0.6

# Hard bound on |command - model desire|, in lateral acceleration. See docstring.
# NOTE it is a BACKSTOP, not the operating point: because beta rises with the
# innovation, the deviation this filter can actually reach is self-limiting and
# peaks around 0.13 m/s^2 (a sustained maneuver at the model's own rate rail
# settles at ~0.10, i.e. ~40 ms behind — under one model frame). The cap exists
# for the case the prediction itself is wrong, e.g. a stale plan across a large
# v_ego change.
DEV_MAX_LAT_ACCEL = 0.15

# Speed floor for the curvature <-> lateral-accel conversions, so the bounds
# stay finite at standstill (they become permissive in curvature terms exactly
# where curvature is meaningless).
V_REF_MIN = 3.0


def _finite(x) -> bool:
  return isinstance(x, (int, float)) and math.isfinite(x)


class KnotFilter:
  """Damps the unpredicted part of each 20 Hz desired-curvature knot."""

  def __init__(self, enabled: bool = True):
    self.enabled = enabled
    self.reset()

  def reset(self) -> None:
    self._offset = 0.0     # curvature units; command - model desire
    self._pred = None      # plan-derived prediction of the NEXT knot
    self._last_raw = None  # fallback prediction when no plan sample is available
    self.deviation = 0.0   # |offset| as lateral accel (m/s^2), for triage/dev UI
    self.beta = 1.0        # last innovation gain, for triage/dev UI

  def set_prediction(self, next_curv_est: float | None) -> None:
    """Store the plan's estimate of the NEXT model action (controlsd's
    `_model_lookahead_curv`). Called once per model frame, after update()."""
    self._pred = float(next_curv_est) if _finite(next_curv_est) else None

  def update(self, model_curv: float, v_ego: float) -> float:
    """Filter one 20 Hz knot. Returns the value to hand to LatSmoother."""
    if not _finite(model_curv):
      # nothing to filter and nothing to learn: leave the state alone so the
      # next real knot is still measured against the last real prediction.
      return model_curv

    raw = float(model_curv)
    if not self.enabled:
      self._last_raw = raw
      self._offset = 0.0
      self.deviation = 0.0
      self.beta = 1.0
      return raw

    v = max(float(v_ego) if _finite(v_ego) else 0.0, V_REF_MIN)
    dev_max = DEV_MAX_LAT_ACCEL / (v * v)
    n_full = N_FULL_LAT_ACCEL / (v * v)

    # Prediction: the previous frame's plan, read one model step past its own
    # action horizon. Falls back to the previous raw action (a constant-
    # curvature process model) only when the plan sample was unavailable —
    # degrading toward, never past, a plain one-frame difference.
    pred = self._pred if self._pred is not None else self._last_raw
    if pred is None:
      # first knot after a reset: nothing to be continuous with, pass through
      self._last_raw = raw
      self._offset = 0.0
      self.deviation = 0.0
      self.beta = 1.0
      return raw

    n = raw - pred
    beta = BETA_MIN + (1.0 - BETA_MIN) * min(abs(n) / n_full, 1.0)

    offset = (1.0 - beta) * (CARRY * self._offset - n)
    offset = min(max(offset, -dev_max), dev_max)

    self._offset = offset
    self._last_raw = raw
    self.beta = beta
    self.deviation = abs(offset) * v * v
    return raw + offset
