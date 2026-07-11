"""FunnyPilot v3.2.10 — LatSmoother: the validated knot interpolation, minimal.

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

This module is that idea and nothing else:

    on each model action (20 Hz):  prev <- last output, cur <- new action
    every control frame (100 Hz):  alpha = clip(elapsed/T_MODEL + PHASE_LEAD, 0, 1)
                                   out = prev + alpha * (cur - prev)

  * PHASE_LEAD = 0.2 reproduces the validated schedule exactly: at a healthy
    100 Hz the five frames emit prev + (0.2, 0.4, 0.6, 0.8, 1.0) * delta.
  * prev is the last OUTPUT, so a late/early model frame can never step the
    command — continuity by construction, at any cadence.
  * time-anchored to the model's FIXED 20 Hz period (never a measured one):
    slow control frames just take proportionally larger alpha increments,
    and if the model stalls, alpha saturates at 1 and the output holds cur.
    No frame counters, no health blends — nothing left to silently degrade.
  * output always lies in [prev, cur]: never outside the model's desire.
  * non-finite model action: hold the current trajectory (knot unchanged).

The ONLY smoothing in the lateral path. clip_curvature (ISO jerk/accel) still
runs downstream as the hard limit. Import-light (stdlib only).
"""
import math

T_MODEL = 0.05    # s, fixed 20 Hz model period (== DT_MDL)
PHASE_LEAD = 0.2  # validated phase advance: first 100 Hz frame emits prev + 0.2*delta
HEALTH_FULL = 5   # dev-UI scale: realized control frames per model frame


class LatSmoother:
  def __init__(self):
    self.reset(0.0)

  def reset(self, curvature: float) -> None:
    c = float(curvature) if math.isfinite(curvature) else 0.0
    self.prev = c
    self.cur = c
    self.out = c
    self._knot_t = None
    self._frames_since_knot = 0
    self._realized_frames = HEALTH_FULL

  @property
  def health_frames(self) -> float:
    """Realized control-frames-per-model-frame (5 healthy), for dev UI/triage."""
    return float(self._realized_frames)

  def update(self, model_curv: float, new_knot: bool, mono_t: float) -> float:
    if new_knot and math.isfinite(model_curv):
      self.prev = self.out  # continuity: never step, whatever the cadence did
      self.cur = float(model_curv)
      self._knot_t = mono_t
      self._realized_frames = min(self._frames_since_knot, HEALTH_FULL)
      self._frames_since_knot = 0
    self._frames_since_knot += 1

    if self._knot_t is None:
      # no knot yet (first frames after engage): pass the model action through
      if math.isfinite(model_curv):
        self.prev = self.cur = self.out = float(model_curv)
      return self.out

    alpha = min(max((mono_t - self._knot_t) / T_MODEL + PHASE_LEAD, 0.0), 1.0)
    self.out = self.prev + alpha * (self.cur - self.prev)
    return self.out
