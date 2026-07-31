"""FunnyPilot v3.2.6e — SCC-Vision v2, rewritten to actually run.

The 3.2.5st version sampled `sm["lateralPlan"]`, a service that no longer
exists — the read threw every frame, the exception handler returned 0.0
predicted lateral acceleration, and SCC-V silently never activated.

This version reads the model plan directly (modelV2.orientationRate.z x
velocity.x, the same signal the proven legacy V-TSC used) and does
POINTWISE corner-speed planning over the plan horizon:

  for every plan point i at time t_i with predicted lat accel a_i > limit:
    corner speed   v_c_i  = v_i * sqrt(a_lat_limit / a_i)
    allowed now    v_now_i = v_c_i + A_DECEL_APPROACH * t_i
  raw cap = min_i(v_now_i)

so a corner 5 s out barely constrains while a corner we are entering
constrains fully — the braking point falls out of the math instead of an
ENTERING/TURNING state machine. Inside the corner (t ~ 0) the cap IS the
corner speed, and CurveSpeedCap rate-limits the release on exit.

FunnyPilot v3.4.9 — TWO CHANGES, both about MISSED corners.

1. THE SELECTION TEST WAS ASKING THE MODEL'S OPINION OF ITS OWN PLAN. A plan
   point counted as a corner only when `orientationRate.z * velocity.x` — the
   lateral accel of the model's OWN planned trajectory — exceeded the comfort
   limit. But `velocity.x` is what the model INTENDS to be doing there, and the
   model plans to slow for corners. So a genuine corner the model already
   planned around read as "under the limit, nothing to do", and SCC-V stayed
   out of it — while in `acc` mode the car does not actually follow that
   planned velocity, so nothing slowed the car at all.

   The corner SPEED never had this problem: v_c = v_i*sqrt(a_max/(rate*v_i)) is
   algebraically sqrt(a_max/curvature), independent of the planned velocity.
   Only the mask was wrong. A point is now a constraint when its corner speed
   is below the speed we are ACTUALLY carrying (max of v_ego and the cruise
   target — the cruise term is what keeps the cap from releasing the moment we
   have slowed to the corner speed, which would oscillate). Strictly more
   sensitive, and identical wherever the model planned to hold speed.

2. CORROBORATION for the merged SCC (scc_fusion.py). A continuous [0, 1] read
   of how much lateral action the model predicts AT OUR SPEED over the plan
   horizon, normalised by CORROB_FRAC of the comfort target and filtered
   asymmetrically (rises fast, falls slowly, so map authority cannot blink out
   mid-corner). This replaces the binary "is SCC-V active" as the thing that
   lets SCC-M act, without weakening the straight-road veto that protects
   against bad map data.

The lateral-accel limit is a comfort target (~2.1 m/s^2 since v3.4.9), scaled by a
BOUNDED factor of the live friction estimate. liveParameters' friction
value is a steering-model parameter, not measured road grip, so it may
only trim the limit +/-30%, never redefine it (the old k_sccv * fric * g
formula demanded 5.6 m/s^2 lateral before acting — beyond most tires).

Speed-domain only: the cap feeds the SpeedGovernor; the MPC + shaper own
the actual deceleration. Import-light: numpy + long_v2 siblings only.
"""
import numpy as np

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CurveSpeedCap, CAP_INACTIVE

try:
  from openpilot.common.params import Params
except Exception:
  Params = None

_DT = 0.05                # planner rate
_V_MIN_ACTIVE = 5.0       # m/s — below this ego speed corner limiting is off
_MIN_V_TARGET = 5.0       # m/s — never command below this for a corner
# v3.4.9: 7.0 -> 8.0 s. The `+ A_DECEL_APPROACH * t` term already de-weights
# distant points to nearly nothing, so widening the window costs no authority
# and buys earlier corroboration for the map (scc_fusion.py).
_MAX_HORIZON_T = 8.0      # s — ignore plan points further out than this
_A_DECEL_APPROACH = 1.2   # m/s^2 — decel budget mapping future corner speed to allowed-now speed
_FRIC_NOMINAL = 0.8
_FRIC_SCALE_MIN = 0.7     # bounded influence: friction estimate can only trim the
_FRIC_SCALE_MAX = 1.1     # comfort lat-accel target, never redefine it
_PARAM_CHECK_FRAMES = 100  # ~5 s @ 20 Hz
_CURV_EPS = 1e-5          # 1/m — below this the plan is straight, not a corner

# v3.4.9 corroboration (see scc_fusion.py). Full corroboration at this fraction
# of the comfort lateral-accel target: a corner that would pull half the comfort
# limit at our current speed is unambiguously a corner, even though SCC-V itself
# has no business slowing for it yet.
CORROB_FRAC = 0.5
_CORROB_ALPHA_UP = 0.5    # rise fast — a corner appearing should grant authority promptly
_CORROB_ALPHA_DOWN = 0.1  # fall slowly — must not blink out mid-corner


def lat_accel_limit(fric: float) -> float:
  tuning = get_tuning()
  scale = min(_FRIC_SCALE_MAX, max(_FRIC_SCALE_MIN, fric / _FRIC_NOMINAL))
  return tuning.a_lat_target * scale


class SCCVisionV2:
  def __init__(self, params=None):
    self.params = params if params is not None else (Params() if Params is not None else None)
    self.frame = -1
    self.enabled = self._read_enabled_param()
    self.state = "INACTIVE"  # INACTIVE / ACTIVE / RELEASING
    self.output_v_target = CAP_INACTIVE
    self.output_a_target = 0.0
    self.is_enabled = False
    self.is_active = False
    self.gas_gating_active = False
    self.raw_v_target = CAP_INACTIVE  # unfiltered, for debug/UI
    # v3.4.9: continuous [0,1] "the model also sees a corner ahead" signal that
    # scales SCC-M's authority (scc_fusion.py). Cleared in _reset(): an SCC-V
    # that is switched off or below its speed floor corroborates nothing, and
    # the map must not inherit a stale agreement from before that.
    self.corroboration = 0.0
    self._cap = CurveSpeedCap(_DT)

  def _read_enabled_param(self) -> bool:
    if self.params is None:
      return True
    try:
      return bool(self.params.get_bool("SmartCruiseControlVision"))
    except Exception:
      return True

  def _raw_cap_from_model(self, model_msg, a_lat_max: float, v_ref: float) -> tuple[float, float]:
    """(minimum allowed-now speed over the plan horizon, corroboration [0,1]).

    Returns (CAP_INACTIVE, corroboration) when nothing constrains us — the
    corroboration is still meaningful there, and is what lets SCC-M act on a
    corner SCC-V has correctly decided it does not need to slow for.
    """
    try:
      rates = np.abs(np.asarray(model_msg.orientationRate.z, dtype=float))
      vels = np.asarray(model_msg.velocity.x, dtype=float)
      t_idxs = np.asarray(model_msg.orientationRate.t, dtype=float)
    except Exception:
      return CAP_INACTIVE, 0.0

    n = min(len(rates), len(vels), len(t_idxs))
    if n == 0:
      return CAP_INACTIVE, 0.0
    rates, vels, t_idxs = rates[:n], vels[:n], t_idxs[:n]

    vels = np.maximum(vels, 1.0)
    mask = (t_idxs <= _MAX_HORIZON_T) & np.isfinite(rates) & np.isfinite(vels)
    # PATH curvature, not the planned trajectory's lateral accel: this is the
    # property of the ROAD, independent of how fast the model intends to take it.
    curv = np.where(mask, rates / vels, 0.0)

    # Corroboration: the lateral accel this path would pull AT OUR SPEED.
    corrob = float(np.max(curv)) * v_ref * v_ref / max(CORROB_FRAC * a_lat_max, 1e-3)
    corrob = float(min(max(corrob, 0.0), 1.0))

    consider = mask & (curv > _CURV_EPS)
    if not np.any(consider):
      return CAP_INACTIVE, corrob

    v_corner = np.sqrt(a_lat_max / curv[consider])
    v_corner = np.maximum(v_corner, _MIN_V_TARGET)
    # A point constrains us when its comfortable corner speed is below the speed
    # we are actually carrying. v_ref uses the cruise target as well as v_ego so
    # the mask does not evaporate the instant we have slowed INTO the corner.
    binding = v_corner < v_ref
    if not np.any(binding):
      return CAP_INACTIVE, corrob

    v_allowed = v_corner[binding] + _A_DECEL_APPROACH * t_idxs[consider][binding]
    return float(np.min(v_allowed)), corrob

  def update(self, sm, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, v_cruise: float, fric: float) -> None:
    self.frame += 1
    if self.frame % _PARAM_CHECK_FRAMES == 0:
      self.enabled = self._read_enabled_param()

    self.is_enabled = long_enabled and self.enabled
    if not self.is_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    a_lat_max = lat_accel_limit(fric)
    v_ref = max(v_ego, v_cruise)
    self.raw_v_target, corrob_raw = self._raw_cap_from_model(sm["modelV2"], a_lat_max, v_ref)
    alpha = _CORROB_ALPHA_UP if corrob_raw > self.corroboration else _CORROB_ALPHA_DOWN
    self.corroboration += (corrob_raw - self.corroboration) * alpha

    cap = self._cap.update(self.raw_v_target, v_ego, v_cruise)
    self.is_active = self._cap.active

    if self.is_active:
      self.state = "RELEASING" if self._cap.releasing else "ACTIVE"
      self.output_v_target = max(cap, _MIN_V_TARGET)
      self.output_a_target = a_ego  # display only; the MPC owns decel
      self.gas_gating_active = v_ego > self.output_v_target + 0.5
    else:
      self.state = "INACTIVE"
      self.output_v_target = CAP_INACTIVE
      self.output_a_target = 0.0
      self.gas_gating_active = False

  def _reset(self):
    self.state = "INACTIVE"
    self.output_v_target = CAP_INACTIVE
    self.output_a_target = 0.0
    self.is_active = False
    self.gas_gating_active = False
    self.raw_v_target = CAP_INACTIVE
    self.corroboration = 0.0
    self._cap.reset()
