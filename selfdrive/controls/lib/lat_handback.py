"""FunnyPilot v3.4.9 — LatHandback: give authority back on a schedule set by the gap.

THE REPORTED CYCLE (right-hand turn, lateral engaged):
  model asks for more right than the driver wants -> driver holds the wheel out
  -> steeringPressed latches -> the override softening cuts total torque to 60%
  -> the driver settles the car on the line they want and relaxes their grip
  -> softening releases -> torque returns to 100% in ~0.15 s WITH THE MODEL'S
     DESIRE UNCHANGED and a frozen integrator still holding pre-override state
  -> the wheel bites right again -> the driver grabs it again.
Once per corner, repeatedly.

The v3.2.8 OverrideGate fixed the WRONG half of this. Its dwell hysteresis
stopped the softening from CHATTERING against wheel-inertia blips, and it did.
But it says nothing about the return: the release is a first-order filter with
a 0.15 s time constant, i.e. essentially a step, and it is the same step
whether the controller was 0.1 m/s^2 away from what the driver had established
or 3 m/s^2 away. The size of that step IS the bite.

THE FIX: the return is a RAMP, and the ramp's duration is scheduled by the
DIVERGENCE between what the model wants and what the car is actually doing at
the moment the driver lets go, measured in lateral acceleration:

    small gap (the corner case)   -> T_SOFT (1.6 s). There is almost nothing to
                                     correct, so taking a long time to take it
                                     costs nothing and removes the bite
                                     entirely.
    large gap (an evasive move)   -> T_FIRM (0.45 s). The car is far off the
                                     model's path; dawdling there is the wrong
                                     trade, so authority comes back briskly.

Interpolated continuously between, and shaped with a smoothstep so the torque
has no corner at either end of the ramp. The DIVERGENCE is peak-held with a
slow bleed across the press, so a driver who happens to be momentarily aligned
at the instant of release still gets the ramp their actual intervention earned.

The second half of the bite is the INTEGRATOR. It is frozen while the driver
presses (CS.steeringPressed), so at handback it still holds whatever it wound
up to before the intervention — mid-corner, that is a demand for more right.
`soft_integrator` keeps it frozen through the first half of the ramp and
latcontrol bleeds it toward zero while the driver is actually in charge, so
the returning authority is feedforward plus a live proportional term rather
than a stored one.

What is deliberately unchanged: the driver ALWAYS wins physically (panda
driver-torque limits and the EPS clamp are untouched); this module only ever
scales the controller's request DOWN, never up; and with no intervention at
all it is an exact no-op (scale == 1.0 on every frame).

Import-light (stdlib only) so it tests without the openpilot environment.
"""
from openpilot.selfdrive.controls.lib.override_gate import OverrideGate

# Total-torque floor while a sustained driver press is in progress. Same value
# and same meaning as the v3.2.3st constant this replaces.
PRESS_SCALE = 0.6
PRESS_TAU = 0.15  # s, first-order approach to the floor when a press engages

# Divergence schedule, in m/s^2 of lateral acceleration between the delayed
# desired lat accel and what the car is measured to be doing.
DIVERGE_LOW = 0.4    # at or below: nothing meaningful to correct
DIVERGE_HIGH = 2.5   # at or above: treat as evasive, return authority briskly
T_SOFT = 1.6         # s, ramp duration at DIVERGE_LOW
T_FIRM = 0.45        # s, ramp duration at DIVERGE_HIGH

# Peak-hold bleed on the divergence measurement, m/s^2 per second. Fast enough
# that a stale spike from earlier in a long press does not dominate, slow
# enough that a momentary alignment right before release does not erase the
# intervention that just happened.
DIVERGE_BLEED = 2.0

# The integrator stays frozen for this fraction of the ramp. Past it the error
# is small enough that letting the integrator work again is what finishes the
# correction rather than what causes a bite.
INTEGRATOR_FREEZE_FRAC = 0.5


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

  @property
  def soft_integrator(self) -> bool:
    """True while the integrator must not be allowed to act on (or store) the
    error the driver's own intervention created."""
    return self.engaged or (self.ramping and self.progress < INTEGRATOR_FREEZE_FRAC)

  def update(self, pressed: bool, desired_lat_accel: float, measured_lat_accel: float) -> float:
    engaged = self._gate.update(bool(pressed))

    gap = abs(float(desired_lat_accel) - float(measured_lat_accel))
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
      self.divergence = self._div_hold
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
      self.scale += (PRESS_SCALE - self.scale) * alpha
    elif self.ramping:
      self._ramp_t += self.dt
      self.progress = min(self._ramp_t / max(self.ramp_duration, self.dt), 1.0)
      self.scale = self._ramp_s0 + (1.0 - self._ramp_s0) * _smoothstep(self.progress)
      if self.progress >= 1.0:
        self.ramping = False
        self.scale = 1.0
    else:
      self.scale = 1.0

    return self.scale
