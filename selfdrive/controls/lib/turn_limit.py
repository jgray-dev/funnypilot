"""FunnyPilot v3.5.4 — anticipatory acceleration limiting in turns.

WHAT WAS THERE. `limit_accel_in_turns` saw a corner only through the MEASURED
steering angle. That makes the limit REACTIVE: the accel ceiling comes down
once the wheel is already turned, i.e. once you are in the bend — which is
precisely when a change in longitudinal accel is least welcome. You accelerate
up to turn-in, then get backed off mid-corner.

WHAT CHANGED. The model already publishes its planned path. Recovering the
geometry from it lets the ceiling come down BEFORE turn-in, so the car simply
stops adding speed into a bend rather than taking it away during one.

THE TRAP, and it is the same one v3.4.9 found in scc_vision_v2: the obvious
quantity is `orientationRate.z * velocity.x`, the lateral accel the MODEL
intends to pull. But the model PLANS TO SLOW for corners, so that product reads
as "nothing to do" exactly where there is something to do. The fix is to
recover the pure geometry —

    curvature = orientation_rate_z / velocity_x        [rad/m]

— which is a property of the ROAD and contains no intent at all, and then
evaluate it at OUR speed:

    a_lat = curvature * v_ego^2

SAFETY POSTURE. The predicted term is combined with the measured one by
`max()`, never replacing it, so this can only ever be MORE conservative than
the pre-v3.5.4 behaviour. Every failure path returns 0.0, which makes the whole
feature a no-op and restores the old numbers bit-for-bit. And it bounds the
accel CEILING only — nothing here can command braking.

Import-light (numpy + ModelConstants) so its tests run without acados or a car.
"""
import math

import numpy as np

from openpilot.common.constants import CV
from openpilot.selfdrive.modeld.constants import ModelConstants

# Total accel budget (long + lat) the car is allowed to spend, by speed.
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]

# How far ahead the limit may look. SHORT ON PURPOSE: the point is to stop
# accelerating INTO a bend that is about to arrive, not to hold the car back
# for one 200 m away. ~2.5 s is roughly the window in which a human lifts off.
TURN_LOOKAHEAD_T = 2.5

# Below this, curvature = rate / velocity is numerically meaningless.
_TURN_V_MIN = 1.0

# ── the exit allowance (v3.7.1) ────────────────────────────────────────────
#
# THE TOTAL BUDGET BELOW 20 m/s IS 1.7 m/s^2, AND THE CORNER BUDGET IS 2.25.
# Those two numbers were chosen in different files for different reasons, and
# together they mean the throttle ceiling is sqrt(max(1.7^2 - 2.25^2, 0)) =
# ZERO for the entire arc of any corner SCC-V/SCC-M is holding at its budget.
# Not "small": zero, until the lateral has fallen a quarter, and then it opens
# as a square root — 0 at 1.7, 0.8 at 1.5. That is the third piece (with the
# cap's flat hold and the runaway release) that made the corner exit a step
# rather than a build: nothing could start until the car had physically left
# the curvature, and then everything let go at once.
#
# THE ALLOWANCE IS KEYED ON THE ROAD OPENING AHEAD, not on a bigger total
# budget. `path_opening` reads the model path: how much the curvature FALLS
# between here and TURN_LOOKAHEAD_T seconds ahead, as a fraction of the
# curvature here. On the entry the curvature ahead is HIGHER, so it is zero; on
# a constant-radius arc it is zero; it is positive only where the model can
# see the bend unwinding — which, on a real corner, is from the apex. The
# ceiling is then floored at EXIT_A_X * opening. Zero by construction wherever
# the limit's original purpose (stop accelerating INTO a bend) applies.
#
# EXIT_A_X 0.6 m/s^2 is about the car's own accel clip at 20 m/s and 70% of it
# at 13, and it sits alongside corner_speed.EXIT_LAT_FRAC — the ramp SCC-M's
# cap now follows from the apex starts at roughly this rate for an ordinary
# bend. The two are the same allowance seen from the speed and the accel side.
EXIT_A_X = 0.6
# Below this much lateral accel there is nothing to open: the sqrt ceiling is
# already wide and the allowance is irrelevant. Keeps the ratio out of the
# noise on a straight road, where k_now is tiny and 1 - k_far/k_now is junk.
_OPENING_A_MIN = 0.5


def predicted_lat_accel(orientation_rate_z, velocity_x, v_ego: float,
                        lookahead_t: float = TURN_LOOKAHEAD_T) -> float:
  """Peak |lateral accel| we will pull within `lookahead_t` if we hold v_ego.

  Returns 0.0 whenever the model has nothing usable, which makes the caller a
  no-op. Takes plain sequences rather than a capnp message so it is testable
  without the model or the device.
  """
  try:
    n = min(len(orientation_rate_z), len(velocity_x), len(ModelConstants.T_IDXS))
    if n < 2 or not math.isfinite(v_ego) or v_ego < _TURN_V_MIN:
      return 0.0
    peak = 0.0
    for i in range(n):
      if ModelConstants.T_IDXS[i] > lookahead_t:
        break
      vi = velocity_x[i]
      rate = orientation_rate_z[i]
      if not (math.isfinite(vi) and math.isfinite(rate)) or vi < _TURN_V_MIN:
        continue
      peak = max(peak, abs(rate / vi) * v_ego * v_ego)
    return peak if math.isfinite(peak) else 0.0
  except Exception:
    return 0.0


def path_opening(orientation_rate_z, velocity_x, v_ego: float,
                 lookahead_t: float = TURN_LOOKAHEAD_T) -> float:
  """How much the road OPENS between here and `lookahead_t`, 0..1. v3.7.1.

  `1 - k_far / k_now`, where k_now is the model path's curvature at the car
  and k_far its curvature at the end of the window (the mean of the last few
  points inside it, because `orientationRate.z` is noisy). Clamped at zero, so
  a road that TIGHTENS ahead — the entry — reads as no opening at all, and a
  constant-radius arc reads as none either. Returns 0.0 for anything unusable,
  and for any lateral accel at our speed under _OPENING_A_MIN, where there is
  no clamped ceiling to open. The 0.0 default makes the caller a no-op.
  """
  try:
    n = min(len(orientation_rate_z), len(velocity_x), len(ModelConstants.T_IDXS))
    if n < 3 or not math.isfinite(v_ego) or v_ego < _TURN_V_MIN:
      return 0.0
    ks = []
    for i in range(n):
      if ModelConstants.T_IDXS[i] > lookahead_t:
        break
      vi, rate = velocity_x[i], orientation_rate_z[i]
      if not (math.isfinite(vi) and math.isfinite(rate)) or vi < _TURN_V_MIN:
        return 0.0
      ks.append(abs(rate / vi))
    if len(ks) < 3:
      return 0.0
    k_now = ks[0]
    if k_now * v_ego * v_ego < _OPENING_A_MIN:
      return 0.0
    tail = ks[-max(2, len(ks) // 4):]
    k_far = sum(tail) / len(tail)
    op = 1.0 - k_far / k_now
    return min(max(op, 0.0), 1.0) if math.isfinite(op) else 0.0
  except Exception:
    return 0.0


def limit_accel_in_turns(v_ego, angle_steers, a_target, CP, a_y_predicted=0.0,
                         opening=0.0):
  """Long accel ceiling given the lateral accel already being spent.

  `a_y_predicted` defaults to 0.0, in which case this is bit-identical to the
  pre-v3.5.4 measured-angle-only version. `opening` (v3.7.1) defaults to 0.0,
  in which case it is bit-identical to v3.5.4; see EXIT_A_X.
  """
  # FIXME (upstream): this lateral accel calculation is approximate and should
  # use the VehicleModel; the lookup table would need updating with it.
  a_total_max = np.interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
  a_y_measured = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  # max(), NOT replace: the anticipatory term may only ever tighten the ceiling.
  a_y = max(abs(a_y_measured), abs(a_y_predicted))
  a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))
  # v3.7.1 — the exit allowance. A FLOOR on the ceiling, never a raise of the
  # total budget: it only matters where the sqrt has clamped to ~0, and it is
  # itself zero unless the model can see the road opening ahead.
  try:
    op = float(opening)
    if math.isfinite(op) and op > 0.0:
      a_x_allowed = max(a_x_allowed, EXIT_A_X * min(op, 1.0))
  except (TypeError, ValueError):
    pass

  return [a_target[0], min(a_target[1], a_x_allowed)]
