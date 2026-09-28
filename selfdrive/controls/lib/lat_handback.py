"""Bounded driver handover, with gentler assistance during small corrections.

Small tracking disagreement and moderate hand force retain partial assistance.
Large disagreement reduces it further; a full-scale opposing K5 driver force
requests zero assistance until release. This is a heuristic, not an inference of intent.
EPS, CarController and Panda driver limits always have final authority.

Repeated short presses pause the return immediately. Greater disagreement earns
more time to return, never a faster pull toward the model's previous path.
"""
import math

from openpilot.selfdrive.controls.lib.override_gate import OverrideGate
from openpilot.selfdrive.controls.lib.eps_limit import STEER_MAX, DRIVER_ALLOWANCE, DRIVER_FACTOR, DRIVER_MULTIPLIER

PRESS_SCALE = 0.6
NUDGE_SCALE = 0.85
PRESS_TAU = 0.15
DIVERGE_LOW = 0.4
DIVERGE_HIGH = 2.5
T_SOFT = 1.6
T_FIRM = 2.5  # greater disagreement: slower handback
DIVERGE_BLEED = 2.0
INTEGRATOR_FREEZE_FRAC = 0.5
# Same K5 sensor units as the existing EPS mirror, not steering-wheel Nm.
# At this force the hardware's opposing-torque allowance reaches zero.
TAKEOVER_TORQUE = (STEER_MAX / DRIVER_MULTIPLIER + DRIVER_ALLOWANCE) / DRIVER_FACTOR
NUDGE_TORQUE = 150.0  # K5 steeringPressed threshold


def _smoothstep(u: float) -> float:
  u = min(max(u, 0.0), 1.0)
  return u * u * (3.0 - 2.0 * u)


def _interp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
  if x <= x0:
    return y0
  if x >= x1:
    return y1
  return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


class LatHandback:
  """Owns the driver-override torque scale, press through return."""

  def __init__(self, dt: float):
    self.dt = dt
    self._gate = OverrideGate(dt)
    self.reset()

  def reset(self) -> None:
    self._gate.reset()
    self.scale = 1.0
    self.engaged = False
    self.ramping = False
    self.progress = 1.0
    self.ramp_duration = 0.0
    self.divergence = 0.0
    self._div_hold = 0.0
    self._ramp_t = 0.0
    self._ramp_s0 = 1.0
    self.takeover = False
    self.press_target = NUDGE_SCALE

  @property
  def soft_integrator(self) -> bool:
    """True while the integrator must not be allowed to act on (or store) the
    error the driver's own intervention created."""
    return self.engaged or (self.ramping and self.progress < INTEGRATOR_FREEZE_FRAC)

  def update(self, pressed: bool, desired_lat_accel: float, measured_lat_accel: float,
             driver_torque: float = 0.0, actuator_torque: float = 0.0) -> float:
    valid = all(math.isfinite(x) for x in (desired_lat_accel, measured_lat_accel, driver_torque, actuator_torque))
    # Strong force bypasses the comfort dwell. Latch the yield until the normal
    # release hysteresis completes; force fluctuations cannot restore torque.
    opposing = driver_torque * actuator_torque < 0.0
    if not valid or (abs(driver_torque) >= TAKEOVER_TORQUE and (opposing or self.takeover)):
      self.takeover = True
      pressed = True
      self._gate.engaged = True
    engaged = self._gate.update(bool(pressed))
    gap = abs(desired_lat_accel - measured_lat_accel) if valid else DIVERGE_HIGH
    force = abs(driver_torque) if valid else TAKEOVER_TORQUE
    severity = max(_interp(gap, DIVERGE_LOW, DIVERGE_HIGH, 0.0, 1.0),
                   _interp(force, NUDGE_TORQUE, TAKEOVER_TORQUE, 0.0, 1.0))
    self.press_target = 0.0 if self.takeover else NUDGE_SCALE + (PRESS_SCALE-NUDGE_SCALE)*severity
    if engaged:
      # peak-hold with a bleed, so the schedule reflects the intervention and
      # not just the instant the driver happened to relax
      self._div_hold = max(gap, self._div_hold - DIVERGE_BLEED * self.dt)

    if engaged and not self.engaged:
      # a press took over: cancel any ramp in progress and go to the floor
      self.ramping = False
      self.progress = 1.0
      self._div_hold = gap
    elif self.engaged and not engaged:
      # RELEASE EDGE: schedule the return from the gap the driver left behind
      self.divergence = max(self._div_hold, DIVERGE_HIGH if self.takeover else 0.0)
      self.takeover = False
      self.ramp_duration = _interp(self.divergence, DIVERGE_LOW, DIVERGE_HIGH, T_SOFT, T_FIRM)
      self._ramp_s0 = self.scale
      self._ramp_t = 0.0
      self.progress = 0.0
      self.ramping = True
      self._div_hold = 0.0

    self.engaged = engaged

    if engaged:
      # first-order approach to the floor: engaging is allowed to be quick,
      # it is the RETURN that has to be scheduled
      alpha = self.dt / max(self.dt, PRESS_TAU)
      # Once yielded, do not increase assistance while the driver still holds
      # the wheel. A calmer measurement alone is not a release request.
      self.scale += (min(self.scale, self.press_target) - self.scale) * alpha
      if self.takeover:
        self.scale = 0.0  # the downstream EPS governor still enforces torque slew
    elif self.ramping:
      if pressed:
        # Even a press shorter than the comfort dwell must stop reapplication.
        self._ramp_s0 = self.scale
        self._ramp_t = 0.0
        self.progress = 0.0
        return self.scale
      self._ramp_t += self.dt
      self.progress = min(self._ramp_t / max(self.ramp_duration, self.dt), 1.0)
      self.scale = self._ramp_s0 + (1.0 - self._ramp_s0) * _smoothstep(self.progress)
      if self.progress >= 1.0:
        self.ramping = False
        self.scale = 1.0
    else:
      self.scale = 1.0

    return self.scale
