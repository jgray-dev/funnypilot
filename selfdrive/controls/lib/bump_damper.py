"""FunnyPilot v3.3.8 — BumpDamper: soften the lateral error channel through a
detected bump, to break the turn-in "grab / fail to hold / grab again" cycle
crossing railroad tracks mid-corner.

HYPOTHESIS STATUS: this is an ACTED-ON hypothesis, not a confirmed fix — user
explicit direction was to stop instrumenting and ship a fix despite the
mechanism being unconfirmed. It is designed to be safe and falsifiable even
if wrong (see the safety argument below and CLAUDE.md for the full writeup).

MECHANISM (co-designed with a second model consult, verified against the
actual code, not just asserted):
  1. get_friction()'s slope inside the deadzone-to-threshold band is
     friction * latAccelFactor / FRICTION_THRESHOLD. For this K5
     (fitted friction 0.1165, FRICTION_THRESHOLD 0.2) that's ~1.6 with the
     fork-locked latAccelFactor of 2.750 — TWICE the PID's own KP of 0.8 —
     and it's live exactly in the small-error turn-in regime.
  2. The fork's locked latAccelFactor (2.750) is ~14% above the K5's fitted
     value (2.405, opendbc/car/torque_data/params.toml). Since torque =
     desired_lat_accel / latAccelFactor, the locked value makes the
     feedforward under-deliver ~12.5% of the torque actually needed for a
     given desired lateral accel — pushing more of the work onto the
     high-gain correction path in (1).
  3. The PID's setpoint is the model's desire from ~lat_delay (~0.5s) ago
     (lat_accel_request_buffer), and the jerk lookahead replays the same
     buffer ~0.3s after a real-world event. A physical disturbance at the
     bump (bump-steer, momentary grip/self-aligning-torque change from
     weight transfer) can corrupt the MEASUREMENT during the event and the
     delay-buffered SETPOINT ~0.3-0.5s later — landing in the middle of the
     high-gain relay above, for long enough to ring for a cycle or two.

FIX: on a detected pitch-rate spike (car-frame Y-axis angular rate — a bump
should show as a coherent nose dip/rebound, reusing controlsd's existing
calibrated IMU pose, zero new subscriptions), blend the measurement toward
the setpoint (shrinking |error|, NOT holding it — holding under a ramping
setpoint would manufacture GROWING error and thus MORE torque, exactly
backwards) for a latched window, and freeze the PID integrator over the same
window. This only ever softens the correction toward the plan's own
feedforward — it cannot add torque or lose the corner, and composes safely
downstream of the EPS governor (torque domain) and panda (hardware backstop).
Falsifiable on the next drive with the EXISTING dev-UI BUMP readout: if the
oscillation still occurs while BUMP shows >TRIGGER_DEG_S (damper provably
engaged), this mechanism is dead and the next suspect is the model's own
plan, not the controller's reaction to it.

Import-light (stdlib only).
"""
import math

TRIGGER_DEG_S = 5.0  # |pitch rate| that declares a bump (baseline <3, confirmed event peak 7)
MIN_DAMP = 0.40      # correction-gain floor while damped (matches the fork's other floors: lane-change 0.45, override 0.6)
HOLD_S = 0.8         # full damping held past the LAST supra-threshold frame (covers the ~0.5s delay-buffer replay + settle)
RECOVER_S = 0.7      # linear recovery back to 1.0 after the hold (total worst case ~1.5s)


def _finite(x) -> bool:
  return isinstance(x, (int, float)) and math.isfinite(x)


class BumpDamper:
  def __init__(self, dt: float):
    self.dt = dt
    self.reset()

  def reset(self) -> None:
    self._hold = 0.0
    self.scale = 1.0

  @property
  def active(self) -> bool:
    return self.scale < 0.999

  def update(self, pitch_rate_deg: float) -> float:
    pr = float(pitch_rate_deg) if _finite(pitch_rate_deg) else 0.0
    if abs(pr) >= TRIGGER_DEG_S:
      # collapse instantly (matches how the EPS governor's bound collapses
      # instantly): a retrigger mid-hold simply extends the hold from here.
      self._hold = HOLD_S
      self.scale = MIN_DAMP
    elif self._hold > 0.0:
      self._hold = max(0.0, self._hold - self.dt)
    else:
      self.scale = min(1.0, self.scale + (1.0 - MIN_DAMP) * self.dt / RECOVER_S)
    return self.scale
