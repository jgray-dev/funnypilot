"""
FunnyPilot LongV2 — following controller (v2.0.2).

When no lead: immediately clear all caps and let the MPC chase v_cruise
naturally. No holdout/ramp — that logic caused edge cases on cold-start
and unnecessary interference on solo driving.

Tier selection: closing-rate-aware brake curve.
  a_req = (v_ego² − v_lead²) / (2 × Δd_to_gap)
This fires gentle decel as soon as we're closing toward the desired gap,
fixing the "coast too late" problem from v2.0.0 TTC-only tiers.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import comfort_scale

_REACCEL_DELAY = 0.4  # s — hold current speed after lead re-appears
_DT = 0.05

# Required-decel tier thresholds [m/s²]
_TIER_T1 = 0.3
_TIER_T2 = 0.8
_TIER_T3 = 1.6
_TIER_T4 = 2.6


class FollowingControllerV2:
  def __init__(self):
    self.state = "CRUISE"
    self.tier = 0
    self.required_decel = 0.0
    self.v_cruise_cap = 999.0
    self.a_override = None
    self.jerk_limit_override = None
    self.plan_source_is_lead = False
    self._reaccel_timer = 0.0
    self._lead2_ff = 0.0

  def _required_decel(self, d_rel: float, v_ego: float, v_lead: float, d_desired: float) -> float:
    if v_ego <= v_lead:
      return 0.0
    delta_d = d_rel - d_desired
    if delta_d <= 1.0:
      return (v_ego ** 2 - v_lead ** 2) / max(2.0, 2.0 * 1.0)
    return (v_ego ** 2 - v_lead ** 2) / (2.0 * delta_d)

  def update(self, sm, v_ego: float, a_ego: float, fric: float) -> None:
    tuning = get_tuning()
    cs = comfort_scale(fric)

    radar = sm["radarState"]
    lead1 = radar.leadOne
    lead2 = radar.leadTwo
    has_lead = lead1.status and lead1.dRel > 0

    # leadTwo feedforward: pre-reduce accel if far-ahead lead is braking hard
    if lead2.status and lead2.aLeadK < -2.0:
      self._lead2_ff = min(self._lead2_ff + 0.3, 0.4)
    else:
      self._lead2_ff = max(self._lead2_ff - 0.1, 0.0)

    if not has_lead:
      # No lead — clear all overrides immediately. MPC handles v_cruise on its own.
      self.state = "CRUISE"
      self.tier = 0
      self.required_decel = 0.0
      self.v_cruise_cap = 999.0
      self.a_override = None
      self.jerk_limit_override = None
      self.plan_source_is_lead = False
      self._reaccel_timer = 0.0
      return

    d_rel = lead1.dRel
    v_lead = lead1.vLead
    a_lead = lead1.aLeadK

    d_desired = v_ego * tuning.thw_default + tuning.d_standstill

    a_req = self._required_decel(d_rel, v_ego, v_lead, d_desired)
    if a_lead < 0:
      a_req = max(a_req, -a_lead * 0.7)

    self.required_decel = a_req

    if a_req >= _TIER_T4:
      self.tier = 4
    elif a_req >= _TIER_T3:
      self.tier = 3
    elif a_req >= _TIER_T2:
      self.tier = 2
    elif a_req >= _TIER_T1:
      self.tier = 1
    else:
      self.tier = 0

    # Stopping / stopped
    if v_ego < 0.5 and d_rel < tuning.d_standstill + 2.0:
      self.state = "STOPPED"
      self.v_cruise_cap = 0.0
      self.a_override = -tuning.decel_comfort * cs
      self.jerk_limit_override = tuning.jerk_limit_safety
      self.plan_source_is_lead = True
      return

    if v_lead < 0.5 and d_rel < tuning.d_standstill + 3.0:
      self.state = "STOPPING"

    if self.tier == 0:
      self.state = "CRUISE" if d_rel > d_desired * 1.5 else "FOLLOWING"
      self.v_cruise_cap = 999.0
      self.a_override = None
      self.jerk_limit_override = None
      self.plan_source_is_lead = (self.state == "FOLLOWING")
    elif self.tier == 1:
      self.state = "FOLLOWING"
      self.v_cruise_cap = v_lead + 0.5
      self.a_override = -(a_req + self._lead2_ff)
      self.jerk_limit_override = tuning.jerk_limit_normal
      self.plan_source_is_lead = True
    elif self.tier == 2:
      self.state = "DECELERATING"
      self.v_cruise_cap = v_lead
      self.a_override = -(a_req + self._lead2_ff)
      self.jerk_limit_override = tuning.jerk_limit_normal * 1.5
      self.plan_source_is_lead = True
    elif self.tier == 3:
      self.state = "DECELERATING"
      self.v_cruise_cap = v_lead - 0.5
      strong_decel = max(a_req, tuning.decel_comfort * cs)
      self.a_override = -(strong_decel + self._lead2_ff)
      self.jerk_limit_override = tuning.jerk_limit_safety
      self.plan_source_is_lead = True
    elif self.tier == 4:
      self.state = "DECELERATING"
      self.v_cruise_cap = max(0.0, v_lead - 1.5)
      hard_decel = max(a_req, tuning.decel_max)
      self.a_override = -(hard_decel + self._lead2_ff)
      self.jerk_limit_override = tuning.jerk_limit_safety * 1.3
      self.plan_source_is_lead = True

    # Brief hold after tier drops back to 0 (prevents instant surge forward)
    if self.tier == 0 and self._reaccel_timer > 0:
      self._reaccel_timer -= _DT
      self.v_cruise_cap = min(self.v_cruise_cap, v_ego)
      self.plan_source_is_lead = True
    elif self.tier > 0:
      self._reaccel_timer = _REACCEL_DELAY
