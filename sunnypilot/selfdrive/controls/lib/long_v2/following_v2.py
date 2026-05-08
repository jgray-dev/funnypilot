"""
FunnyPilot LongV2 — following state machine with predictive TTC and tiered decel.
"""
import math

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.fric import comfort_scale

_G = 9.81
_TTC_TIER1 = 4.0   # s — start following
_TTC_TIER2 = 2.5   # s — moderate decel
_TTC_TIER3 = 1.5   # s — strong decel
_TTC_TIER4 = 0.8   # s — AEB
_REACCEL_DELAY = 0.4      # s — wait after lead lost before releasing cruise cap
_LEAD_LOST_HOLDOUT = 1.2  # s — hold speed cap after lead disappears
_LEAD_LOST_RAMP = 2.0     # s — ramp back to v_cruise after holdout
_DT = 0.05                # control loop dt


class FollowingControllerV2:
  def __init__(self):
    self.state = "CRUISE"
    self.v_cruise_cap = 999.0
    self.a_override = None      # None = let MPC decide
    self.jerk_limit_override = None
    self.plan_source_is_lead = False
    self._reaccel_timer = 0.0
    self._lead_lost_timer = 0.0
    self._lead_lost_v_hold = None
    self._lead2_ff = 0.0

  def _kinematic_ttc(self, d_rel: float, v_rel: float) -> float:
    """Predictive TTC using kinematic model. v_rel = v_lead - v_ego (negative = closing)."""
    if v_rel >= 0 or d_rel <= 0:
      return 999.0
    return d_rel / (-v_rel)

  def update(self, sm, v_ego: float, a_ego: float, fric: float) -> None:
    tuning = get_tuning()
    cs = comfort_scale(fric)

    radar = sm["radarState"]
    lead1 = radar.leadOne
    lead2 = radar.leadTwo
    has_lead = lead1.status and lead1.dRel > 0

    # Lead-two feedforward: if lead2 is braking hard, pre-reduce accel
    if lead2.status and lead2.aLeadK < -2.0:
      self._lead2_ff = min(self._lead2_ff + 0.3, 0.3)
    else:
      self._lead2_ff = max(self._lead2_ff - 0.1, 0.0)

    if not has_lead:
      self._handle_lead_lost(v_ego, tuning)
      return

    # Reset lead-lost state
    self._lead_lost_timer = 0.0
    self._lead_lost_v_hold = None

    d_rel = lead1.dRel
    v_lead = lead1.vLead
    v_rel = v_lead - v_ego
    a_lead = lead1.aLeadK

    ttc = self._kinematic_ttc(d_rel, v_rel)

    # Desired gap based on THW
    d_desired = v_ego * tuning.thw_default + tuning.d_standstill

    # State machine
    if ttc < _TTC_TIER4:
      self.state = "DECELERATING"
      tier = 4
    elif ttc < _TTC_TIER3:
      self.state = "DECELERATING"
      tier = 3
    elif ttc < _TTC_TIER2 or d_rel < d_desired * 0.7:
      self.state = "DECELERATING"
      tier = 2
    elif d_rel < d_desired:
      self.state = "FOLLOWING"
      tier = 1
    else:
      self.state = "FOLLOWING"
      tier = 0

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

    # Compute v_cruise_cap and a_override by tier
    if tier == 0:
      self.v_cruise_cap = 999.0
      self.a_override = None
      self.jerk_limit_override = None
      self.plan_source_is_lead = False
    elif tier == 1:
      # Gently follow
      self.v_cruise_cap = v_lead + 0.5
      self.a_override = None
      self.jerk_limit_override = tuning.jerk_limit_normal
      self.plan_source_is_lead = True
    elif tier == 2:
      self.v_cruise_cap = v_lead
      comfort_decel = tuning.decel_comfort * cs * 0.6
      self.a_override = max(-comfort_decel - self._lead2_ff, a_ego - 1.0)
      self.jerk_limit_override = tuning.jerk_limit_normal * 2
      self.plan_source_is_lead = True
    elif tier == 3:
      self.v_cruise_cap = v_lead - 1.0
      comfort_decel = tuning.decel_comfort * cs * 0.9
      self.a_override = -comfort_decel - self._lead2_ff
      self.jerk_limit_override = tuning.jerk_limit_safety
      self.plan_source_is_lead = True
    elif tier == 4:
      self.v_cruise_cap = max(0.0, v_lead - 2.0)
      self.a_override = -(tuning.decel_comfort * cs * 1.2 + self._lead2_ff)
      self.jerk_limit_override = tuning.jerk_limit_safety * 1.5
      self.plan_source_is_lead = True

    # REACCEL delay: if was decelerating and tier drops back to 0
    if tier == 0 and self._reaccel_timer > 0:
      self._reaccel_timer -= _DT
      self.v_cruise_cap = v_ego  # hold current speed during delay
      self.plan_source_is_lead = True
    elif tier > 0:
      self._reaccel_timer = _REACCEL_DELAY

  def _handle_lead_lost(self, v_ego: float, tuning) -> None:
    if self._lead_lost_v_hold is None:
      self._lead_lost_v_hold = v_ego
      self._lead_lost_timer = _LEAD_LOST_HOLDOUT + _LEAD_LOST_RAMP

    self._lead_lost_timer -= _DT

    if self._lead_lost_timer > _LEAD_LOST_RAMP:
      # Holdout phase: cap at last known speed
      self.state = "CRUISE"
      self.v_cruise_cap = self._lead_lost_v_hold
      self.a_override = 0.0
      self.jerk_limit_override = tuning.jerk_limit_normal
      self.plan_source_is_lead = True
    elif self._lead_lost_timer > 0:
      # Ramp phase: linearly release cap
      frac = self._lead_lost_timer / _LEAD_LOST_RAMP
      self.v_cruise_cap = self._lead_lost_v_hold + (999.0 - self._lead_lost_v_hold) * (1 - frac)
      self.a_override = None
      self.jerk_limit_override = tuning.jerk_limit_normal
      self.plan_source_is_lead = False
      self.state = "REACCEL"
    else:
      # Fully released
      self._lead_lost_v_hold = None
      self.v_cruise_cap = 999.0
      self.a_override = None
      self.jerk_limit_override = None
      self.plan_source_is_lead = False
      self.state = "CRUISE"
