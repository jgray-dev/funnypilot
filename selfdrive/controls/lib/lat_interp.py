"""
FunnyPilot v3.2.2: robust, cadence-independent lateral curvature interpolation.

The driving model publishes a delay-compensated desired curvature at 20 Hz
(MODEL_RUN_FREQ); controlsd runs the lateral loop at 100 Hz. We interpolate the
model's curvature across the 5 control frames between model updates so the wheel
moves in many small steps instead of a few big "bites".

Two methods:

  LINEAR  - the validated uniform delta/5 feel (3.1.0e+), re-expressed as a
            TIME-ANCHORED ramp. It is driven by elapsed wall-clock time within
            the model frame, not by counting control frames, so it can no longer
            silently degrade when the 100:20 Hz cadence drifts under thermal/CPU
            load. Bit-compatible with the old feel at a healthy 100 Hz.

  SETTLE  - delay-aware smoothing (DEFAULT). Full responsiveness INTO a corner
            (no added turn-in lag — it is never below the LINEAR path), then a
            gentle ease-out as the model's desired curvature flattens toward the
            apex/exit, so the wheel settles rather than arriving in a jerk
            impulse. The "is it flattening?" decision uses a one-model-step
            lookahead sampled from the model's own published plan — free compute
            available inside the actuator/software-delay window.

Why this fixes the "interp deactivates after offroad/reboot" bug:
  The previous implementation counted control frames and assumed exactly 5 per
  model frame. When controlsd's effective rate drops toward the model rate
  (thermal throttle / load, which build up the longer the device runs), the
  frame counter never advanced and the command froze at ~20% of every step. Here
  the phase is real elapsed time over the model's FIXED 20 Hz period, and a
  health term blends toward the model's raw desire when sub-frame headroom is
  lost — so the worst case degrades to stock openpilot, never to a laggy stall.

Safety invariants (unit-tested in tests/test_lat_interp.py):
  * output is always within [min(prev,cur), max(prev,cur)] — never "outside" the
    model's desire — and equals cur exactly at the model frame (alpha -> 1). The
    model is always the reference at its own control frames.
  * SETTLE output is always >= the LINEAR output toward cur — it never adds lag.
  * non-finite inputs fall back to the last good model desire.
clip_curvature() in controlsd still runs afterward as the hard jerk/accel limit.
"""
import math

# The road model is camera-locked at MODEL_RUN_FREQ = 20 Hz, so the model period
# is a FIXED true value (== DT_MDL). Anchoring the interpolation phase to
# elapsed-time / this-fixed-period — rather than a measured/EMA period — avoids
# the chronic-lag failure mode where an inflated period estimate makes every
# segment stop short of the model's desire.
T_MODEL = 0.05  # s, == common.realtime.DT_MDL

# Preserve the validated phase advance: the old frame-counted scheme emitted
# prev + 0.2*delta at the model frame (index 0 of 5). alpha = elapsed/T + LEAD
# reproduces that exactly at 100 Hz while staying correct at any loop rate.
PHASE_LEAD = 0.2

# Control-frames-per-model-frame at which interpolation has full sub-frame
# headroom (100/20 = 5; >= 4 counts as healthy). Below this we blend toward the
# raw model desire so we degrade to stock, never to a laggy stall.
HEALTH_FULL_FRAMES = 4.0

# Below this speed the model's curvature (= lat_accel / v^2) and any plan-derived
# lookahead get large/noisy, so SETTLE falls back to LINEAR.
SETTLE_MIN_SPEED = 6.7  # m/s (~15 mph, matches the smooth-stop boundary)

# Hard sanity bound on any curvature we reason about (== drive_helpers.MAX_CURVATURE).
MAX_CURVATURE = 0.2

LINEAR = "linear"
SETTLE = "settle"


def _finite(x) -> bool:
  return isinstance(x, (int, float)) and math.isfinite(x)


class LatInterp:
  def __init__(self, method: str = SETTLE):
    self.method = method
    self._prev = 0.0         # previous model-frame desired curvature (knot)
    self._cur = 0.0          # current  model-frame desired curvature (knot)
    self._prev_delta = 0.0   # delta of the segment we just left (settle fallback)
    self._t0: float | None = None   # monotonic time the current model frame arrived
    self._frames = 0         # control frames elapsed in the current segment
    self._health = 1.0       # realized frames-per-model / HEALTH_FULL_FRAMES, [0,1]
    self._w = 0.0            # settle ease-out weight, latched per segment [0,1]
    self._seeded = False

  @property
  def health_frames(self) -> float:
    # realized control-frames-per-model-frame (for the dev-UI health indicator)
    return self._health * HEALTH_FULL_FRAMES

  def reset(self, curvature: float) -> None:
    # Called while lateral is inactive: drop any in-flight segment so a re-engage
    # starts clean (no stale knots, no alpha-saturation spike). The first active
    # frame re-seeds to the live model desire, exactly like the old scheme.
    c = curvature if _finite(curvature) else 0.0
    self._prev = self._cur = float(c)
    self._prev_delta = 0.0
    self._t0 = None
    self._frames = 0
    self._w = 0.0
    self._seeded = False

  def update(self, model_desired: float, model_updated: bool, now: float,
             vego: float, next_curv_est: float | None = None) -> float:
    if not _finite(model_desired):
      model_desired = self._cur  # bad frame -> hold last good model desire

    # First active frame (or right after reset): seed both knots to the model
    # desire and pass it straight through. clip_curvature ramps from the measured
    # curvature, matching the validated re-engage behavior.
    if self._t0 is None or not self._seeded:
      self._prev = self._cur = float(model_desired)
      self._prev_delta = 0.0
      self._t0 = now
      self._frames = 0
      self._health = 1.0
      self._w = 0.0
      self._seeded = True
      return float(model_desired)

    if model_updated:
      # Close the segment that just finished -> realized sub-frame headroom.
      self._health = min(max(self._frames / HEALTH_FULL_FRAMES, 0.0), 1.0)
      # Advance the knots.
      self._prev_delta = self._cur - self._prev
      self._prev = self._cur
      self._cur = float(model_desired)
      self._t0 = now
      self._frames = 1
      self._w = self._settle_weight(vego, next_curv_est)
    else:
      self._frames += 1

    delta = self._cur - self._prev
    alpha = (now - self._t0) / T_MODEL + PHASE_LEAD
    alpha = 0.0 if alpha < 0.0 else (1.0 if alpha > 1.0 else alpha)

    if self.method == SETTLE and self._w > 0.0:
      # g(alpha) = alpha + w*alpha*(1-alpha): an ease-out that is ALWAYS >= alpha
      # (>= the linear path, so never laggier) and ALWAYS within [0,1] (so the
      # value stays in the [prev,cur] bracket), reaching 1 exactly at alpha=1.
      g = alpha + self._w * alpha * (1.0 - alpha)
    else:
      g = alpha

    val = self._prev + g * delta

    # LOAD-BEARING safety clamp: never outside the model's [prev, cur] bracket.
    # This — not the curve shape — is what guarantees the invariant. Do not remove.
    lo = self._prev if delta >= 0.0 else self._cur
    hi = self._cur if delta >= 0.0 else self._prev
    val = lo if val < lo else (hi if val > hi else val)

    # Degraded-cadence guard: when the control loop loses sub-frame headroom,
    # blend toward the raw model desire so we degrade to stock, not to a stall.
    out = self._health * val + (1.0 - self._health) * self._cur

    return out if _finite(out) else self._cur

  def _settle_weight(self, vego: float, next_curv_est: float | None) -> float:
    # 0 -> pure linear (full responsiveness); 1 -> full ease-out settle.
    if self.method != SETTLE:
      return 0.0
    if not _finite(vego) or vego < SETTLE_MIN_SPEED:
      return 0.0
    delta = self._cur - self._prev
    if abs(delta) < 1e-6:
      return 0.0
    # Prefer the model's own lookahead (where its curvature is heading next);
    # fall back to the previous segment's delta if it is unavailable/unsafe.
    if _finite(next_curv_est) and abs(next_curv_est) <= MAX_CURVATURE:
      next_delta = next_curv_est - self._cur
    else:
      next_delta = self._prev_delta
    r = next_delta / delta  # >=1 deepening; (0,1) flattening; <=0 reversing
    w = 1.0 - r
    return 0.0 if w < 0.0 else (1.0 if w > 1.0 else w)
