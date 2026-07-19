"""FunnyPilot v3.3.7 — SLA pre-zone speed ramps (pure math, import-light).

Approaching a LOWER speed limit zone the v3.3.3 gas gate only clamps throttle
to coast (~0.35 m/s^2), which is not always enough to arrive at the new
target — the MPC then brakes abruptly at the boundary. Approaching a HIGHER
zone nothing happened until the boundary, where the set-speed snap released
the full acceleration authority at once ("all hell breaks loose").

Both are the same problem mirrored: the speed target should RAMP through the
transition so the car crosses the boundary already AT the new zone's target
(which includes the SLA dynamic offset ratio — callers pass the ratio-adjusted
targets). Constant-accel envelopes, exactly the SCC-M approach math:

  decel (next < current):
      d_eff      = max(0, d - v_next * LEAD_T)         # arrive early
      v_allowed  = sqrt(v_next^2 + 2 * PLAN_DECEL * d_eff)
    min-picked with the current zone target: far away it exceeds the current
    target (no-op); inside the braking envelope it ramps down and equals
    v_next at (LEAD_T seconds before) the boundary. The MPC does the braking;
    the existing coast gas gate still engages even earlier.

  accel (next > current):
      v_allowed  = sqrt(max(v_next^2 - 2 * PLAN_ACCEL * d, v_cur^2))
    equals the current target until d = (v_next^2 - v_cur^2)/(2*PLAN_ACCEL),
    then ramps up and reaches v_next exactly at the boundary — the transition
    accel is PLAN_ACCEL by construction, not the full personality surge.
"""
import math

PLAN_DECEL = 0.8       # m/s^2 — planned pre-zone deceleration (braking, via the MPC)
PLAN_ACCEL_UP = 0.5    # m/s^2 — planned pre-zone acceleration into a higher zone
DECEL_LEAD_T = 1.5     # s — reach the lower target this early (matches the gas gate buffer)


def pre_zone_decel_target(v_next: float, distance: float,
                          plan_decel: float = PLAN_DECEL, lead_t: float = DECEL_LEAD_T) -> float:
  """Allowed-now speed while approaching a lower zone; inf when unconstrained."""
  if not (math.isfinite(v_next) and math.isfinite(distance)) or v_next < 0.0 or distance <= 0.0:
    return float("inf")
  d_eff = max(0.0, distance - v_next * lead_t)
  return math.sqrt(v_next * v_next + 2.0 * plan_decel * d_eff)


def pre_zone_accel_target(v_cur: float, v_next: float, distance: float,
                          plan_accel: float = PLAN_ACCEL_UP) -> float:
  """Allowed-now speed while approaching a higher zone; ramps v_cur -> v_next."""
  if not (math.isfinite(v_cur) and math.isfinite(v_next) and math.isfinite(distance)):
    return v_cur
  if v_next <= v_cur or v_cur < 0.0 or distance < 0.0:
    return v_cur
  v_sq = v_next * v_next - 2.0 * plan_accel * distance
  return math.sqrt(max(v_sq, v_cur * v_cur))
