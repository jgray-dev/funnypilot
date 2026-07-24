"""FunnyPilot v3.3.9 — SlaSpeedRamp: predictive, mode-agnostic speed-domain
smoothing for Speed Limit Assist zone transitions.

WHY: SLA's target steps at the zone boundary (v3.3.3 deliberately removed the
resolver's early limit-switch so the POSTED limit doesn't change early). That
step works fine feeding the 'acc'-mode MPC, whose cruise-obstacle formulation
(X_EGO_OBSTACLE_COST) turns any step target into a smooth approach by
construction. It does NOT work well feeding 'blended' mode (active whenever
DEC picks it, or full E2E is on): blended mode's cost weights are
[0., 0.1, 0.2, 5.0, a_change_cost, 1.0] — position tracking (0.1) and
velocity tracking (0.2) are an order of magnitude below the model's own
acceleration-plan tracking (5.0), so the model's own plan actively competes
with a stepped v_cruise cap rather than being weakly dominated by it. A
smoothly PRE-RAMPED target keeps the cap-vs-plan gap small at every instant,
which is a regime the weak weights can actually close.

USER DIRECTIVE (explicit, supersedes the v3.3.3-era assumption that the MPC
"cannot brake before entering the new zone" — see longitudinal_planner.py's
updated comment): rather than fight DEC's own acc/blended mode selection
heuristics, shape v_cruise itself, upstream of the MPC, so the fix works
identically regardless of which mode ends up active.

MECHANISM: a constant-decel envelope, same shape as scc_map_v2.py's
_raw_cap_from_map (sqrt form — distance-based, no v_ego division, so it is
standstill-safe by construction, unlike a naive distance/v_ego time term):

    d_eff = max(0, next_distance - next_target * ARRIVAL_LEAD_T)
    predictive = sqrt(next_target^2 + 2 * A_DECEL * d_eff)
    candidate = min(current_target, predictive)

This engages automatically as next_distance shrinks and converges exactly to
next_target at the boundary. Deliberately NO predictive up-ramp: raising the
cap before the boundary would command overspeed in the current, slower zone,
and in blended mode a cap can only ever restrain the model's plan, never
accelerate it — raising it early buys nothing. The up-direction transition
(entering a FASTER zone) is handled entirely by the per-frame release-rate
limiter below, which only starts moving once the CURRENT zone's own target
has already stepped up at the boundary — i.e. purely post-boundary.

A short dropout hold (HOLD_S) covers the resolver's per-frame zeroing of
next_speed_limit_final/distance_to_next_limit whenever live map data isn't
currently available (stale GPS fix, momentary gap), so a brief data flicker
doesn't sawtooth the ramp. KNOWN LIMITATION: this predictive shaping only
functions with map-source (ahead) limit data; car-state (dash-recognized)
limits have no lookahead distance and fall back to a plain step at the
boundary, same as the pre-existing gas gate.

Composes with the existing pre-zone gas gate (accel-domain, THROTTLE-ONLY,
GATE_COAST_ACCEL=0.35) without conflict: the gate's envelope is tighter
(engages closer to the boundary than this ramp's A_DECEL=1.0 comfort budget
would), so the car coasts first, then this ramp adds gentle commanded decel
closer in — sequenced automatically by their different constants, no
coordination needed. SLA's own zone/ratio state machine is completely
untouched by this module; it is a pure post-processing shaper.

Import-light (stdlib only).
"""
import math

A_DECEL = 1.0            # m/s^2, comfort decel budget (matches long_v2 A_DECEL_APPROACH for map)
ARRIVAL_LEAD_T = 2.0      # s, arrive at the next zone's target this early (matches scc_map_v2)
RELEASE_RATE_UP = 0.6     # m/s^2, post-boundary acceleration release rate (in-family with A_CRUISE_MAX at speed)
RELEASE_RATE_DOWN = 2.0   # m/s^2, generous backstop against next-target data jumps; the envelope itself is gentler
HOLD_S = 2.5              # s, hold last-valid next-zone data through a brief resolver dropout
CAP_INACTIVE = 999.0      # matches long_v2 convention


def _finite_positive(x: float) -> bool:
  return isinstance(x, (int, float)) and math.isfinite(x) and x > 0.0


class SlaSpeedRamp:
  def __init__(self, dt: float):
    self.dt = dt
    self.reset()

  def reset(self) -> None:
    self._out = CAP_INACTIVE
    self._held_next_target = 0.0
    self._held_distance = 0.0
    self._hold_timer = 0.0

  def update(self, is_active: bool, current_target: float, next_target: float,
             next_distance: float, v_ego: float) -> float:
    if not is_active or not _finite_positive(current_target):
      self.reset()
      return CAP_INACTIVE

    if _finite_positive(next_target) and _finite_positive(next_distance):
      self._held_next_target = next_target
      self._held_distance = next_distance
      self._hold_timer = HOLD_S
    elif self._hold_timer > 0.0:
      self._hold_timer = max(0.0, self._hold_timer - self.dt)
      self._held_distance = max(0.0, self._held_distance - max(v_ego, 0.0) * self.dt)
    else:
      self._held_next_target = 0.0
      self._held_distance = 0.0

    candidate = current_target
    if 0.0 < self._held_next_target < current_target:
      d_eff = max(0.0, self._held_distance - self._held_next_target * ARRIVAL_LEAD_T)
      predictive = math.sqrt(self._held_next_target ** 2 + 2.0 * A_DECEL * d_eff)
      candidate = min(current_target, predictive)
    # deliberately no predictive up-ramp — see module docstring

    if self._out == CAP_INACTIVE:
      self._out = candidate  # seed clean on activation, no step
    elif candidate < self._out:
      self._out = max(candidate, self._out - RELEASE_RATE_DOWN * self.dt)
    else:
      self._out = min(candidate, self._out + RELEASE_RATE_UP * self.dt)
    return self._out
