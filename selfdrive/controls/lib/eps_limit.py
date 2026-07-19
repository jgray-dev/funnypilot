"""FunnyPilot v3.3.8 — EpsTorqueGovernor: request only what the rack will do.

THE MECHANISM (2021 K5 DL3, classic-CAN HKG): the carcontroller — and the
panda, identically — clamp the commanded LKAS torque by the DRIVER-torque
limit (opendbc/car/lateral.py apply_driver_steer_torque_limits):

    allowed = STEER_MAX + (ALLOWANCE - |opposing sensor torque| * FACTOR) * MULT
    (K5: 384 + (50 - |torque|) * 2, slewed at +3/-7 per 10 ms frame)

HYPOTHESIS STATUS (be honest with the next reader): the torsion-bar sensor
doesn't only read the driver's hands — wheel-inertia reaction torque during
a hard self-steer bite can spike it too. The v3.2.8-era analysis INFERRED
readings of 150+ from steeringPressed latching; that was NEVER verified
on-road (the 3.2.8 "fix" built on it did not cure the oscillation — user
confirmed). What IS certain is the clamp itself: it exists, its threshold
is a sensor reading of just 50 (at 150 the allowance is 184/384 — half
authority), and it is invisible to the tuning layer. IF the sensor crosses
50 during self-steer, the loop is: bite -> sensor spikes -> clamp sheds
torque at -7/frame -> wheel decelerates -> sensor relaxes -> clamp
releases -> the controller, still demanding full torque, bites again at
+3/frame -> repeat, re-excited by every model knot. The triage "dtx"
(max |raw sensor|/s), "eps" (min authority/s) and "tqd" (max
requested-vs-applied divergence/s) fields exist to CONFIRM OR FALSIFY this
on-road — read them before iterating on this module.

THE FIX — mirror the hardware limits inside the controller, with damping:

  1. DRIVER-LIMIT MIRROR: compute the exact carcontroller bound from
     CS.steeringTorque each frame and never request beyond it. Requested
     torque == realizable torque, so the PID no longer winds against a
     clamp it cannot see.
  2. DAMPED RECOVERY: the bound is honored instantly on the way DOWN
     (matching the hardware, which clamps regardless), but recovers at
     RECOVERY_RATE — ~2x slower than the hardware would allow — so the
     post-clamp re-application cannot re-excite the sensor. This is the
     term that breaks the limit cycle: the hardware loop's gain lives in
     the fast re-bite.
  3. SLEW MIRROR: the request also follows the hardware's +3/-7-per-frame
     slew from the last request, so a model knot can never ask the rack
     for a bite it physically will not take this frame.

The governor can only ever REDUCE torque relative to the unclamped demand;
panda's hardware enforcement is untouched and remains the safety backstop.
While the driver bound is limiting, latcontrol freezes the PID integrator
(anti-windup) and feeds the saturation alert (sustained authority loss in a
corner must stay driver-visible — same principle as v3.3.5's soundd note).

Constants mirror opendbc/car/hyundai/values.py CarControllerParams for the
K5's default classic-CAN branch (this fork is K5-only; cf. locked LAF).
Import-light (stdlib only).
"""
import math

# Mirrors of opendbc hyundai CarControllerParams (K5 2021 default branch) —
# if opendbc changes these, update them together.
STEER_MAX = 384.0
STEER_DELTA_UP = 3.0      # units per 10 ms frame, toward higher magnitude
STEER_DELTA_DOWN = 7.0    # units per 10 ms frame, toward zero
DRIVER_ALLOWANCE = 50.0
DRIVER_FACTOR = 1.0
DRIVER_MULTIPLIER = 2.0
HW_FRAME_DT = 0.01        # the 100 Hz frame the hardware rates are defined on

# Authority recovery after a driver-limit clamp, in full-scale fraction per
# second. Hardware would allow STEER_DELTA_UP/STEER_MAX/HW_FRAME_DT = 0.78/s;
# recovering at less than half that is the damping that breaks the
# bite -> clamp -> re-bite limit cycle.
RECOVERY_RATE = 0.35


def _finite(x) -> bool:
  return isinstance(x, (int, float)) and math.isfinite(x)


class EpsTorqueGovernor:
  def __init__(self, dt: float):
    self.dt = dt
    self._up = STEER_DELTA_UP / STEER_MAX * (dt / HW_FRAME_DT)
    self._down = STEER_DELTA_DOWN / STEER_MAX * (dt / HW_FRAME_DT)
    self._recovery = RECOVERY_RATE * dt
    self.reset()

  def reset(self) -> None:
    self.last_out = 0.0     # last requested torque, actuator frame, [-1, 1]
    self._ceil = 1.0        # damped positive authority bound
    self._floor = -1.0      # damped negative authority bound
    self.driver_limited = False
    self.authority = 1.0    # bound magnitude on the demanded side (for triage)

  def update(self, t_des: float, driver_torque: float) -> float:
    """t_des: desired torque in the ACTUATOR frame, normalized [-1, 1]
    (the same frame/sign as CS.steeringTorque and actuators.torque)."""
    if not _finite(t_des):
      t_des = self.last_out
    drv = float(driver_torque) if _finite(driver_torque) else 0.0

    # 1. exact hardware driver-limit bounds, normalized. Aligned sensor torque
    # extends the bound (never past full scale); opposing torque shrinks it.
    driver_max = (STEER_MAX + (DRIVER_ALLOWANCE + drv * DRIVER_FACTOR) * DRIVER_MULTIPLIER) / STEER_MAX
    driver_min = (-STEER_MAX + (-DRIVER_ALLOWANCE + drv * DRIVER_FACTOR) * DRIVER_MULTIPLIER) / STEER_MAX
    max_allowed = max(min(1.0, driver_max), 0.0)
    min_allowed = min(max(-1.0, driver_min), 0.0)

    # 2. damped bound tracking: collapse instantly (the hardware clamps this
    # frame no matter what we request), recover at RECOVERY_RATE.
    self._ceil = min(max_allowed, min(self._ceil + self._recovery, 1.0))
    self._floor = max(min_allowed, max(self._floor - self._recovery, -1.0))

    t_bounded = min(max(t_des, self._floor), self._ceil)
    self.driver_limited = abs(t_des - t_bounded) > 1e-9
    self.authority = self._ceil if t_des >= 0.0 else -self._floor

    # 3. hardware slew mirror (same shape as apply_driver_steer_torque_limits:
    # DELTA_UP away from zero, DELTA_DOWN toward zero, sign crossings bounded).
    last = self.last_out
    if last > 0.0:
      out = min(max(t_bounded, max(last - self._down, -self._up)), last + self._up)
    else:
      out = min(max(t_bounded, last - self._up), min(last + self._down, self._up))

    self.last_out = out
    return out
