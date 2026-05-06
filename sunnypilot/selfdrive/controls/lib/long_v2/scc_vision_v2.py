"""
FunnyPilot LongV2 — SCC-Vision v2 (physics-aware corner detection).
"""
import math
import numpy as np

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning

_G = 9.81
_V_MIN_ACTIVE = 5.0       # m/s — below this speed ignore corner limiting
_LAT_ACCEL_LOOKAHEAD = 3  # seconds of model lookahead to sample
_ENTERING_THRESHOLD_FACTOR = 1.0
_LEAVING_THRESHOLD_FACTOR = 0.7
_MIN_V_TARGET = 5.0


class SCCVisionV2:
  def __init__(self):
    self.state = "INACTIVE"  # INACTIVE / ENTERING / TURNING / LEAVING
    self.output_v_target = 999.0
    self.output_a_target = 0.0
    self.is_enabled = False
    self.is_active = False
    self.gas_gating_active = False
    self._v_target_smooth = 999.0

  def _lat_accel_threshold(self, fric: float) -> float:
    tuning = get_tuning()
    return tuning.k_sccv * fric * _G

  def _p97_predicted_lat_accel(self, sm) -> float:
    try:
      plan = sm["lateralPlan"]
      curvatures = list(plan.curvatures)
      v_plan = list(sm["longitudinalPlan"].speeds) if sm.updated.get("longitudinalPlan") else []
      if not curvatures:
        return 0.0
      v_ego = sm["carState"].vEgo
      # Use v_ego for curvature → lat_accel if no speed plan
      speeds = v_plan[:len(curvatures)] if v_plan else [max(v_ego, 1.0)] * len(curvatures)
      lat_accels = [abs(c) * max(v, 1.0) ** 2 for c, v in zip(curvatures, speeds)]
      if not lat_accels:
        return 0.0
      return float(np.percentile(lat_accels, 97))
    except Exception:
      return 0.0

  def update(self, sm, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float, fric: float) -> None:
    tuning = get_tuning()
    threshold = self._lat_accel_threshold(fric)

    self.is_enabled = long_enabled
    if not long_enabled or v_ego < _V_MIN_ACTIVE:
      self._reset()
      return

    p97_lat = self._p97_predicted_lat_accel(sm)

    entering_thresh = threshold * _ENTERING_THRESHOLD_FACTOR
    leaving_thresh = threshold * _LEAVING_THRESHOLD_FACTOR

    if self.state == "INACTIVE":
      if p97_lat > entering_thresh:
        self.state = "ENTERING"
    elif self.state == "ENTERING":
      if p97_lat > leaving_thresh:
        self.state = "TURNING"
      else:
        self.state = "INACTIVE"
    elif self.state == "TURNING":
      if p97_lat < leaving_thresh:
        self.state = "LEAVING"
    elif self.state == "LEAVING":
      if p97_lat < leaving_thresh * 0.8:
        self.state = "INACTIVE"
      elif p97_lat > entering_thresh:
        self.state = "TURNING"

    # Always compute v_target from curvature (visible in debug UI even when inactive)
    kappa = p97_lat / max(v_ego ** 2, 1.0)
    if kappa > 1e-4:
      v_target_raw = math.sqrt(threshold / kappa)
    else:
      v_target_raw = 999.0
    v_target_raw = max(_MIN_V_TARGET, v_target_raw)

    # Smooth v_target: slow on the way down, fast on the way up
    if v_target_raw < self._v_target_smooth:
      alpha = 0.15
    else:
      alpha = 0.4
    self._v_target_smooth = self._v_target_smooth * (1 - alpha) + v_target_raw * alpha

    self.output_v_target = self._v_target_smooth

    if self.state in ("ENTERING", "TURNING", "LEAVING"):
      self.is_active = True
      self.output_a_target = a_ego
      self.gas_gating_active = v_ego > self.output_v_target + 1.0
    else:
      self.is_active = False
      self.output_a_target = 0.0
      self.gas_gating_active = False

  def _reset(self):
    self.state = "INACTIVE"
    self.output_v_target = 999.0
    self.output_a_target = 0.0
    self.is_active = False
    self.gas_gating_active = False
    self._v_target_smooth = 999.0
