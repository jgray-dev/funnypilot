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

The lateral-accel limit is a comfort target (~2.4 m/s^2), scaled by a
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
_MAX_HORIZON_T = 7.0      # s — ignore plan points further out than this
_A_DECEL_APPROACH = 1.2   # m/s^2 — decel budget mapping future corner speed to allowed-now speed
_FRIC_NOMINAL = 0.8
_FRIC_SCALE_MIN = 0.7     # bounded influence: friction estimate can only trim the
_FRIC_SCALE_MAX = 1.1     # comfort lat-accel target, never redefine it
_PARAM_CHECK_FRAMES = 100  # ~5 s @ 20 Hz


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
    self._cap = CurveSpeedCap(_DT)

  def _read_enabled_param(self) -> bool:
    if self.params is None:
      return True
    try:
      return bool(self.params.get_bool("SmartCruiseControlVision"))
    except Exception:
      return True

  def _raw_cap_from_model(self, model_msg, a_lat_max: float) -> float:
    """Minimum allowed-now speed over the plan horizon; CAP_INACTIVE if unconstrained."""
    try:
      rates = np.abs(np.asarray(model_msg.orientationRate.z, dtype=float))
      vels = np.asarray(model_msg.velocity.x, dtype=float)
      t_idxs = np.asarray(model_msg.orientationRate.t, dtype=float)
    except Exception:
      return CAP_INACTIVE

    n = min(len(rates), len(vels), len(t_idxs))
    if n == 0:
      return CAP_INACTIVE
    rates, vels, t_idxs = rates[:n], vels[:n], t_idxs[:n]

    vels = np.maximum(vels, 1.0)
    mask = (t_idxs <= _MAX_HORIZON_T) & np.isfinite(rates) & np.isfinite(vels)
    lat_accels = rates * vels
    over = mask & (lat_accels > a_lat_max)
    if not np.any(over):
      return CAP_INACTIVE

    v_corner = vels[over] * np.sqrt(a_lat_max / lat_accels[over])
    v_corner = np.maximum(v_corner, _MIN_V_TARGET)
    v_allowed = v_corner + _A_DECEL_APPROACH * t_idxs[over]
    return float(np.min(v_allowed))

  def update(self, sm, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, v_cruise: float, fric: float) -> None:
    self.frame += 1
    if self.frame % _PARAM_CHECK_FRAMES == 0:
      self.enabled = self._read_enabled_param()

    self.is_enabled = long_enabled and self.enabled
    if not self.is_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    a_lat_max = lat_accel_limit(fric)
    self.raw_v_target = self._raw_cap_from_model(sm["modelV2"], a_lat_max)

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
    self._cap.reset()
