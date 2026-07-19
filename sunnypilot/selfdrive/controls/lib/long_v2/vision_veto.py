"""FunnyPilot v3.3.7 — vision veto of SCC-M.

Some locations have wrong map data (or a mapd calculation bug) and SCC-M
slows the car on a road that is dead straight. The model can see that: if
SCC-M's governing curve point is INSIDE the model's reliable vision horizon
and the model's own plan predicts near-zero lateral acceleration all the way
through it, the map constraint is bogus — ignore it and keep the current /
SCC-V speed.

Design rules (fail toward NOT vetoing — a wrong veto removes a safety
slowdown, a missed veto is only a comfort bug):

  * only veto when the curve is CLOSE (time-to-curve < TRUST_T): beyond the
    vision horizon the map legitimately knows about curves the model cannot
    see yet, and that earliness is the whole point of SCC-M;
  * the model must be actively straight: max predicted lat accel over its
    horizon below STRAIGHT_FRAC of the SCC-V comfort limit. Unknown /
    non-finite straightness (broken plan) never arms;
  * SCC-V must itself be unconstrained (not active) — if vision wants to
    slow too, the corner is real;
  * armed only after ARM_FRAMES consecutive straight frames (~1 s), released
    immediately when the model sees any meaningful curvature (RELEASE_FRAC)
    or the preconditions lapse.

Import-light (stdlib only) so tests run without the openpilot environment.
"""
import math

VETO_TRUST_T = 4.5        # s — only veto curves within this time (vision can see them)
VETO_STRAIGHT_FRAC = 0.40  # arm: max predicted lat accel < this fraction of the limit
VETO_RELEASE_FRAC = 0.60   # release: any predicted lat accel above this fraction
VETO_ARM_FRAMES = 20       # consecutive straight frames before the veto engages (~1 s @ 20 Hz)


class SccmVisionVeto:
  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self._straight_frames = 0
    self.active = False

  def update(self, sccm_constraining: bool, curve_distance_m: float, v_ego: float,
             vision_ok: bool, sccv_active: bool, max_pred_lat_accel: float,
             a_lat_limit: float) -> bool:
    """Returns True while SCC-M's cap should be ignored."""
    if not (sccm_constraining and vision_ok and not sccv_active
            and math.isfinite(max_pred_lat_accel) and a_lat_limit > 0.0
            and curve_distance_m > 0.0):
      self.reset()
      return False

    t_to_curve = curve_distance_m / max(v_ego, 1.0)
    if t_to_curve >= VETO_TRUST_T:
      self.reset()
      return False

    frac = max_pred_lat_accel / a_lat_limit
    if self.active:
      # release the moment the model sees real curvature ahead
      if frac >= VETO_RELEASE_FRAC:
        self.reset()
      return self.active

    if frac < VETO_STRAIGHT_FRAC:
      self._straight_frames += 1
      if self._straight_frames >= VETO_ARM_FRAMES:
        self.active = True
    else:
      self._straight_frames = 0
    return self.active
