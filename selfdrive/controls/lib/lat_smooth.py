"""FunnyPilot v3.3.6 — LatSmoother: validated knot timing, C1-smooth in-period shape.

POST-MORTEM of v3.2.9e (PlanRider): sampling the model plan at an advancing
horizon looked principled, but the horizon formula (2*psi/(v*t) - psi_rate/v)
is nearly t-INVARIANT inside a curve — advancing the horizon between model
frames barely moved the output, and each new plan then delivered the whole
50 ms of turn progression as one step. That is the stock 20 Hz staircase
reborn, with its largest steps exactly in sharp turns ("two 45-degree bites
instead of ten 9-degree ones"). Deleted.

What months of on-road use actually validated (3.1.0e -> 3.2.1st) was the
simple thing: spread the DELTA between consecutive model actions uniformly
across the model period, so the wheel provably moves EVERY control frame by
delta/5 — smoothness by construction, not by hoping a formula cooperates.

v3.3.6 keeps that schedule's TIMING contract untouched and smooths its SHAPE:

    on each model action (20 Hz):  prev <- last output, cur <- new action
    every control frame (100 Hz):  alpha = clip(elapsed/T_MODEL + PHASE_LEAD, 0, 1)
                                   out = prev + g(alpha) * (cur - prev)

  LINEAR: g(alpha) = alpha — the validated delta/5 ramp, unchanged.

  SPLINE (default): g is a monotone cubic Hermite whose entry slope is the
  slope the output ACTUALLY had at the end of the previous segment, and whose
  exit slope is aimed at where the model's own published plan says the desire
  goes one model step past the action horizon (a pure read of the plan inside
  the lagd delay window — free compute, no filtering). Both slopes are clamped
  to the Fritsch–Carlson monotone box [0, 3]*secant, which guarantees:
    * g is monotone 0 -> 1, so the output stays inside [prev, cur] and lands
      on cur EXACTLY when the linear ramp would — the model's knot values and
      knot times are never altered, so maneuver onset cannot shift;
    * on a constant-rate maneuver (equal consecutive deltas) the carried
      slope equals the secant and g collapses to alpha — bit-identical to the
      validated delta/5 schedule. The spline only differs where the linear
      scheme kinked: rate CHANGES become continuous (C1) instead of 20 Hz
      slope steps, and a flattening plan eases the wheel into the apex
      (the 3.2.2 SETTLE feel emerges from the clamped exit slope);
    * a flat segment (delta == 0) stays EXACTLY flat — no creep, ever.

  * PHASE_LEAD = 0.2 keeps the validated phase: at a healthy 100 Hz the five
    frames evaluate g at (0.2, 0.4, 0.6, 0.8, 1.0).
  * prev is the last OUTPUT, so a late/early model frame can never step the
    command — continuity by construction, at any cadence.
  * time-anchored to the model's FIXED 20 Hz period (never a measured one):
    slow control frames just take proportionally larger alpha increments,
    and if the model stalls, alpha saturates at 1 and the output holds cur.
    No frame counters, no health blends — nothing left to silently degrade.
  * output always lies in [prev, cur]: never outside the model's desire
    (hard-clamped even though the monotone construction already implies it).
  * non-finite model action: hold the current trajectory (knot unchanged).
    non-finite/absent lookahead: exit slope falls back to the plain secant.

v3.3.2 NOTE (still binding): the v3.2.12 smooth_seconds_for_delay budget (EMA
funded by the delay window, action horizon pulled earlier by tau) lived here
and is REMOVED. On-road verdict: an EMA redistributes each maneuver across the
window instead of delaying it — partial steering appeared before the old
decisive onset ("a 2.5 in the space before the 5"), and raising the delay
knob only smeared it further. Do not reintroduce causal filtering on the
knots. The SPLINE above is NOT that: it filters nothing — every knot value is
reached at its validated time and flats stay flat; only the sub-period path
between unchanged knots is shaped, using future information (the plan), never
past outputs.

v3.3.7 — INNOVATION ABSORB (on top of the spline; knot-level, method-agnostic):

The spline made the sub-period path smooth but still executes EVERY 20 Hz knot
exactly — so a model plan REVISION (the "complete 180" between consecutive
model updates) reaches the wheel at full amplitude in one model period. No
in-period interpolation can fix that; the harshness lives in the knot sequence
itself. The fix must not be knot filtering either (the v3.2.12 EMA post-mortem
above is still binding).

The split that threads the needle: every new model action is decomposed
against what the model's OWN previously-published plan predicted for this
exact instant (the same lookahead sample the spline already reads —
get_curvature_from_plan at lat_delay + 2*DT_MDL predicts the next action's
horizon point):

    innovation = new_action - previous_plan_prediction

  * The PREDICTED component executes EXACTLY on the validated schedule.
    Genuine maneuvers (curve entry, lane change) appear in the plan seconds
    before their action — plan-consistent motion is untouched, so onset stays
    decisive, with zero added delay and zero pre-onset creep. This is not a
    filter: nothing is averaged, no horizon is shifted.
  * The SURPRISE component (the gated innovation excess — precisely the
    revision/noise the wheel should not chase) is carried as a deficit and
    released linearly across the lagd delay window (lateralDelay). During
    that window the vehicle has not yet physically responded to the previous
    command, so revising over it instead of instantly is free — and the
    command provably converges to the raw model request within lateralDelay
    of the last surprise, so the model never sees a persistent tracking
    error (no over-correct/ping-pong spiral: we stay on the course the model
    itself published, and blend to its revision inside the dead-time).
  * A flip-flop CANCELS: model flips (+X surprise absorbed), next frame flips
    back (-X surprise absorbed) — the deficits annihilate and the wheel
    barely moves through the whole oscillation. This is the felt fix for
    "the next model update is a complete 180."

  Gating: innovation within (INNOV_GATE_REL * |predicted step| +
  INNOV_GATE_ABS) passes through exactly — small prediction noise and any
  systematic plan-vs-action bias (which scales with maneuver intensity) never
  accumulates a deficit, so steady cornering is bit-compatible with the
  validated schedule. Only the excess beyond the gate is absorbed.

  Safety invariants (all hard-clamped, not just constructed):
    * the effective target always lies BETWEEN the course we are on and the
      raw model request — the command only ever moves toward the model's
      current desire, never past it, never away from it;
    * |deficit| <= max_absorb (a lateral-acceleration budget from controlsd,
      ABSORB_LATACC_MAX / v^2): anything larger — an emergency swerve —
      executes immediately except the capped remainder;
    * the deficit fully releases within max(lateralDelay, ABSORB_WINDOW_MIN)
      of its last growth (linear release, rate fixed at absorption — no
      geometric tail, no residual bias);
    * max_absorb = 0 (the default) disables the layer bit-exactly.
  clip_curvature (ISO jerk/accel) still runs downstream as the hard limit.

The ONLY smoothing in the lateral path. Import-light (stdlib only).
"""
import math

T_MODEL = 0.05    # s, fixed 20 Hz model period (== DT_MDL)
PHASE_LEAD = 0.2  # validated phase advance: first 100 Hz frame evaluates g(0.2)
HEALTH_FULL = 5   # dev-UI scale: realized control frames per model frame

# Fritsch–Carlson monotonicity box: normalized end slopes in [0, 3] keep the
# cubic Hermite monotone on [0, 1] (g' is bilinear in (c0, c1) at fixed alpha,
# so its minimum over the box is at a corner; all four corners give g' >= 0).
FC_MAX = 3.0

# Innovation-absorb gate (v3.3.7): innovation within REL*|predicted step| + ABS
# executes exactly (validated feel, no deficit from plan-vs-action bias); only
# the excess beyond it is absorbed. ABS sits at the old INTERP "curvy" delta
# threshold — sub-perceptual revisions pass untouched.
INNOV_GATE_REL = 1.0
INNOV_GATE_ABS = 5e-5     # 1/m
ABSORB_WINDOW_MIN = 0.10  # s, release-window floor if lagd reports a tiny delay

LINEAR = "linear"
SPLINE = "spline"


class LatSmoother:
  def __init__(self, method: str = SPLINE):
    self.method = method
    self.reset(0.0)

  def reset(self, curvature: float) -> None:
    c = float(curvature) if math.isfinite(curvature) else 0.0
    self.prev = c
    self.cur = c
    self.out = c
    self._knot_t = None
    self._frames_since_knot = 0
    self._realized_frames = HEALTH_FULL
    self._slope = 0.0  # realized output slope (curvature/s), carried for C1 entry
    self._sat = 0      # consecutive frames with alpha saturated at 1 (model stall)
    self._c0 = 1.0     # normalized entry slope of the current segment
    self._c1 = 1.0     # normalized exit slope of the current segment
    self._raw_cur = c        # the model's raw (undeficited) current request
    self._pred_next = None   # previous plan's prediction of the incoming knot
    self._absorb = 0.0       # surprise deficit: raw request minus effective target
    self._release = 0.0      # deficit release rate, 1/m per s (fixed at absorption)

  @property
  def health_frames(self) -> float:
    """Realized control-frames-per-model-frame (5 healthy), for dev UI/triage."""
    return float(self._realized_frames)

  @property
  def absorb(self) -> float:
    """Current surprise deficit (1/m): raw model request minus effective target."""
    return float(self._absorb)

  def update(self, model_curv: float, new_knot: bool, mono_t: float,
             next_curv_est: float | None = None,
             lat_delay: float = 0.2, max_absorb: float = 0.0) -> float:
    if new_knot and math.isfinite(model_curv):
      raw = float(model_curv)
      self.prev = self.out  # continuity: never step, whatever the cadence did

      # ---- innovation absorb (v3.3.7; see module docstring) ----
      # release first, then absorb the new surprise: a flip-back's innovation
      # lands on the still-held deficit of the flip and cancels it.
      if self._absorb != 0.0:
        self._absorb -= math.copysign(min(abs(self._absorb), self._release * T_MODEL), self._absorb)
      if self._pred_next is not None:
        innov = raw - self._pred_next
        gate = INNOV_GATE_REL * abs(self._pred_next - self._raw_cur) + INNOV_GATE_ABS
        if abs(innov) > gate:
          self._absorb += math.copysign(abs(innov) - gate, innov)
      cap = max(float(max_absorb), 0.0) if math.isfinite(max_absorb) else 0.0
      self._absorb = min(max(self._absorb, -cap), cap)
      # effective target: raw minus deficit, hard-clamped BETWEEN the course we
      # are on and the raw request — only ever toward the model's desire.
      lo, hi = (self.prev, raw) if raw >= self.prev else (raw, self.prev)
      cur_eff = min(max(raw - self._absorb, lo), hi)
      self._absorb = raw - cur_eff  # bookkeeping matches the clamp
      # release rate: current deficit fully gone within the lagd delay window
      # of this (its most recent) growth. Linear — no geometric tail.
      self._release = max(self._release, abs(self._absorb) / max(float(lat_delay), ABSORB_WINDOW_MIN))
      if self._absorb == 0.0:
        self._release = 0.0
      self._raw_cur = raw
      # store the plan's RAW prediction of the next action for the next split
      self._pred_next = float(next_curv_est) if (next_curv_est is not None and math.isfinite(next_curv_est)) else None
      # ---- end innovation absorb ----

      self.cur = cur_eff
      self._knot_t = mono_t
      self._realized_frames = min(self._frames_since_knot, HEALTH_FULL)
      self._frames_since_knot = 0
      # aim the spline's exit slope along the EFFECTIVE course (deficit-adjusted
      # lookahead), so the in-period shape and the absorb layer agree.
      est = self._pred_next - self._absorb if self._pred_next is not None else None
      self._set_segment_shape(est)
    self._frames_since_knot += 1

    if self._knot_t is None:
      # no knot yet (first frames after engage): pass the model action through
      if math.isfinite(model_curv):
        self.prev = self.cur = self.out = self._raw_cur = float(model_curv)
      return self.out

    alpha = min(max((mono_t - self._knot_t) / T_MODEL + PHASE_LEAD, 0.0), 1.0)
    delta = self.cur - self.prev
    if self.method == SPLINE:
      g, dg = self._hermite(alpha)
    else:
      g, dg = alpha, 1.0
    val = self.prev + g * delta

    # LOAD-BEARING clamp: never outside the model's [prev, cur] desire bracket.
    # The monotone construction already implies it; this is the hard guarantee.
    lo, hi = (self.prev, self.cur) if delta >= 0.0 else (self.cur, self.prev)
    self.out = min(max(val, lo), hi)

    # realized slope, carried into the next segment as its C1 entry slope. At
    # alpha == 1 (the normal last frame of a healthy segment) dg is exactly the
    # aimed exit slope c1 — carry it, so on-time knots chain C1. Only a genuine
    # model stall (alpha saturated for more than one frame, output holding cur)
    # decays the carried slope to zero: the wheel really has stopped moving.
    self._sat = self._sat + 1 if alpha >= 1.0 else 0
    self._slope = dg * delta / T_MODEL if self._sat <= 1 else 0.0
    return self.out

  def _set_segment_shape(self, next_curv_est: float | None) -> None:
    delta = self.cur - self.prev
    if self.method != SPLINE or abs(delta) < 1e-9:
      # flat (or linear-method) segment: g(alpha) = alpha; with delta ~= 0 the
      # output holds prev exactly — a flat desire can never creep.
      self._c0 = self._c1 = 1.0
      return
    secant = delta / T_MODEL

    # entry slope: what the output was actually doing. A direction change (or
    # entry from a hold) restarts from zero slope — monotone into the bracket.
    m0 = self._slope if self._slope * secant > 0.0 else 0.0

    # exit slope: aim at the plan's next knot estimate (central difference).
    # Unavailable/non-finite/reversing -> plain secant / zero, degrading the
    # spline toward the validated linear ramp, never past it.
    if next_curv_est is not None and math.isfinite(next_curv_est):
      m1 = (float(next_curv_est) - self.prev) / (2.0 * T_MODEL)
      if m1 * secant <= 0.0:
        m1 = 0.0  # plan says we're at/past the apex: settle to zero slope
    else:
      m1 = secant

    self._c0 = min(m0 / secant, FC_MAX)
    self._c1 = min(m1 / secant, FC_MAX)

  def _hermite(self, a: float) -> tuple[float, float]:
    # cubic Hermite basis on [0,1]: g(0)=0, g(1)=1, g'(0)=c0, g'(1)=c1.
    # Monotone (g' >= 0) for c0, c1 in [0, FC_MAX] — see docstring.
    om = 1.0 - a
    g = self._c0 * a * om * om + a * a * (3.0 - 2.0 * a) + self._c1 * a * a * (a - 1.0)
    dg = self._c0 * om * (1.0 - 3.0 * a) + 6.0 * a * om + self._c1 * a * (3.0 * a - 2.0)
    return g, dg
