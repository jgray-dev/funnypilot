"""
FunnyPilot LongV2 — following controller (v2.0.1).

v2.0.1 rewrite: Closing-rate-aware brake curve.

Old behavior (v2.0.0): tier triggered on TTC < N seconds. With small Δv
(e.g. ego 50 mph closing on lead 42 mph), TTC stays >10s until you're
right on top of the lead — so the controller does nothing and the MPC
coasts. The user feels "we coast too late and for too long".

New behavior: compute the constant deceleration required to match the
lead's speed by the time we reach the desired headway distance. If
that required decel exceeds tier thresholds, brake at that rate
immediately. This pre-empts the coast and feels natural.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import comfort_scale

_DT = 0.05
_REACCEL_DELAY = 0.4
_LEAD_LOST_HOLDOUT = 1.2
_LEAD_LOST_RAMP = 2.0

# Required-decel tier thresholds [m/s²]
_TIER_T1 = 0.3   # tier 1: gentle
_TIER_T2 = 0.8   # tier 2: moderate
_TIER_T3 = 1.6   # tier 3: strong
_TIER_T4 = 2.6   # tier 4: hard / AEB-adjacent


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
    self._lead_lost_timer = 0.0
    self._lead_lost_v_hold = None
    self._lead2_ff = 0.0

  def _required_decel(self, d_rel: float, v_ego: float, v_lead: float, d_desired: float) -> float:
    """
    Constant decel needed so that we reach v_lead exactly when our gap
    has closed to d_desired. Returns 0 if not closing or already past.
    Formula: a = (v_ego² - v_lead²) / (2 × Δd)
    """
    if v_ego <= v_lead:
      return 0.0
    delta_d = d_rel - d_desired
    if delta_d <= 1.0:
      # Already inside desired gap — derive an aggressive decel to recover
      return (v_ego ** 2 - v_lead ** 2) / max(2.0, 2.0 * 1.0)
    return (v_ego ** 2 - v_lead ** 2) / (2.0 * delta_d)

  def update(self, sm, v_ego: float, a_ego: float, fric: float) -> None:
    tuning = get_tuning()
    cs = comfort_scale(fric)

    radar = sm["radarState"]
    lead1 = radar.leadOne
    lead2 = radar.leadTwo
    has_lead = lead1.status and lead1.dRel > 0

    # leadTwo feedforward: pre-reduce accel if a far-ahead lead is braking hard
    if lead2.status and lead2.aLeadK < -2.0:
      self._lead2_ff = min(self._lead2_ff + 0.3, 0.4)
    else:
      self._lead2_ff = max(self._lead2_ff - 0.1, 0.0)

    if not has_lead:
      self._handle_lead_lost(v_ego, tuning)
      return

    # Lead present
    self._lead_lost_timer = 0.0
    self._lead_lost_v_hold = None

    d_rel = lead1.dRel
    v_lead = lead1.vLead
    a_lead = lead1.aLeadK

    # Desired following gap based on time headway
    d_desired = v_ego * tuning.thw_default + tuning.d_standstill

    a_req = self._required_decel(d_rel, v_ego, v_lead, d_desired)
    # Account for lead's own braking — we need to brake at least as hard
    if a_lead < 0:
      a_req = max(a_req, -a_lead * 0.7)

    self.required_decel = a_req

    # Tier classification by required deceleration
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

    # Stopping / stopped detection
    if v_ego < 0.5 and d_rel < tuning.d_standstill + 2.0:
      self.state = "STOPPED"
      self.v_cruise_cap = 0.0
      self.a_override = -tuning.decel_comfort * cs
      self.jerk_limit_override = tuning.jerk_limit_safety
      self.plan_source_is_lead = True
      return

    if v_lead < 0.5 and d_rel < tuning.d_standstill + 3.0:
      self.state = "STOPPING"

    # Apply tier
    if self.tier == 0:
      self.state = "CRUISE" if d_rel > d_desired * 1.5 else "FOLLOWING"
      self.v_cruise_cap = 999.0
      self.a_override = None
      self.jerk_limit_override = None
      self.plan_source_is_lead = (self.state == "FOLLOWING")
    elif self.tier == 1:
      self.state = "FOLLOWING"
      # Cap at lead speed + small margin; gentle decel command
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

    # REACCEL grace period
    if self.tier == 0 and self._reaccel_timer > 0:
      self._reaccel_timer -= _DT
      self.v_cruise_cap = min(self.v_cruise_cap, v_ego)
      self.plan_source_is_lead = True
    elif self.tier > 0:
      self._reaccel_timer = _REACCEL_DELAY

  def _handle_lead_lost(self, v_ego: float, tuning) -> None:
    self.required_decel = 0.0
    self.tier = 0
    if self._lead_lost_v_hold is None:
      self._lead_lost_v_hold = v_ego
      self._lead_lost_timer = _LEAD_LOST_HOLDOUT + _LEAD_LOST_RAMP

    self._lead_lost_timer -= _DT

    if self._lead_lost_timer > _LEAD_LOST_RAMP:
      self.state = "CRUISE"
      self.v_cruise_cap = self._lead_lost_v_hold
      self.a_override = 0.0
      self.jerk_limit_override = tuning.jerk_limit_normal
      self.plan_source_is_lead = True
    elif self._lead_lost_timer > 0:
      frac = self._lead_lost_timer / _LEAD_LOST_RAMP
      self.v_cruise_cap = self._lead_lost_v_hold + (999.0 - self._lead_lost_v_hold) * (1 - frac)
      self.a_override = None
      self.jerk_limit_override = tuning.jerk_limit_normal
      self.plan_source_is_lead = False
      self.state = "REACCEL"
    else:
      self._lead_lost_v_hold = None
      self.v_cruise_cap = 999.0
      self.a_override = None
      self.jerk_limit_override = None
      self.plan_source_is_lead = False
      self.state = "CRUISE"
