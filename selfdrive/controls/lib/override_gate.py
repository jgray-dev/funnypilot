"""FunnyPilot v3.2.8 — driver-override gate.

Fixes the "bite then loosen" lateral oscillation. The v3.2.3st override
softening scaled TOTAL steering torque to 60% the instant CS.steeringPressed
latched. But steeringPressed is just torsion-bar torque over a threshold
(HKG: 150 counts for 5 frames) — and a hard steering bite can cross that
WITHOUT a driver override: the wheel rim's own inertia (or a lightly resting
hand) resists the rapid acceleration and twists the bar. That closes a limit
cycle: full torque -> wheel accelerates -> bar twists -> "pressed" -> torque
cut to 60% + integrator frozen -> wheel decelerates -> bar relaxes ->
"pressed" clears -> full torque bites again, at a few Hz.

The gate breaks the cycle with dwell-time hysteresis:
  * ENGAGE only after steeringPressed has been continuously true for
    ENGAGE_TIME. Inertia blips during a bite last ~0.1-0.25 s and alternate
    with releases, so they never qualify. A real takeover is a sustained
    press and engages after ENGAGE_TIME (the driver still always wins
    physically — panda driver-torque limits and the EPS are unaffected).
  * RELEASE only after steeringPressed has been continuously false for
    RELEASE_TIME, so a genuine override doesn't flicker at the threshold.

Either way the softening can no longer alternate frame-to-frame: it is
engaged (sustained fight -> steady 60%) or it is off (steady 100%).

Import-light (stdlib only) so tests run without the openpilot environment.
"""

ENGAGE_TIME = 0.4   # s of continuous steeringPressed before softening engages
RELEASE_TIME = 0.3  # s of continuous release before softening lets go


class OverrideGate:
  def __init__(self, dt: float):
    self.dt = dt
    self.reset()

  def reset(self) -> None:
    self.engaged = False
    self._pressed_t = 0.0
    self._released_t = 0.0

  def update(self, pressed: bool) -> bool:
    if pressed:
      self._pressed_t += self.dt
      self._released_t = 0.0
    else:
      self._released_t += self.dt
      self._pressed_t = 0.0

    if not self.engaged and self._pressed_t >= ENGAGE_TIME:
      self.engaged = True
    elif self.engaged and self._released_t >= RELEASE_TIME:
      self.engaged = False
    return self.engaged
