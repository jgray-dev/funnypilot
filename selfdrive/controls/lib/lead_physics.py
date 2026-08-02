"""FunnyPilot v3.5.5 — how much room a lead vehicle actually needs to stop.

THE DEFECT THIS FIXES IS AN INCONSISTENT PHYSICAL ASSUMPTION, not a tuning
opinion. The MPC turns a moving lead into a stationary obstacle by adding the
lead's own stopping distance to its position:

    obstacle = x_lead + v_lead^2 / (2 * COMFORT_BRAKE)

`COMFORT_BRAKE` is OUR comfortable deceleration. Using it here asserts that the
lead will also decelerate at 2.2 m/s^2 — no matter what the lead is observably
doing. When the lead brakes harder than that (which is most of what a real stop
looks like: 3-5 m/s^2), the term over-estimates how far the lead will travel,
so the obstacle is placed too far away and we start braking too late. Having
started late we then need MORE than COMFORT_BRAKE to recover, which is the
"too hard, too late" the driver feels. It is not a subjective judgement:

    v_ego = v_lead = 20 m/s, lead braking at 4.0 m/s^2, t_follow 1.6 s
      lead travels          400 / (2*4.0) = 50.0 m
      we need to stop in    400 / (2*2.2) = 90.9 m, plus STOP_DISTANCE 7.5
      -> the gap we must already have is 98.4 - 50.0 = 48.4 m

    what the old constant asks for:
      obstacle = x_lead + 90.9, and the MPC wants
      (x_lead + 90.9) - x_ego >= 90.9 + 1.6*20 + 7.5   ->   gap >= 39.5 m

39.5 < 48.4, so the geometry is short before a single frame is solved, and the
shortfall is made up in a harder-than-comfortable stop.

THE FIX IS TO BELIEVE THE LEAD. Use the deceleration the lead is ACTUALLY
running, floored at COMFORT_BRAKE:

    decel = clip(-a_lead, COMFORT_BRAKE, LEAD_DECEL_MAX)

BOUNDED SO IT CAN ONLY EVER HELP, WHICH IS THE WHOLE SAFETY ARGUMENT:

  * FLOORED AT COMFORT_BRAKE. A lead that is coasting, holding speed, or
    braking more gently than we would returns EXACTLY the old number, so this
    is a bit-for-bit no-op in the case the driver explicitly asked not to
    change — "not constantly braking when following a lead vehicle simply
    slowing down". Nothing about ordinary following moves.
  * A larger believed decel only ever makes the equivalence distance SMALLER,
    which moves the obstacle CLOSER, which can only make us brake EARLIER.
    There is no input that makes this brake later than today.
  * CAPPED AT LEAD_DECEL_MAX. Not for safety — believing a bigger number is
    the conservative direction — but because `aLeadK` is a Kalman output on a
    radar track and a momentary spike would otherwise yank the obstacle
    tens of metres closer for one frame, i.e. a brake jab. The cap plus the
    filter below are what keep this from becoming the thing it is meant to
    remove. Beyond the cap, AEB/FCW is the layer that owns the outcome.

This module is deliberately stdlib-only: `long_mpc.py` imports acados and
therefore cannot be constructed off-device, so anything that needs a test lives
out here (same reason as turn_limit.py and long_shaping.py).
"""

# How hard we are willing to believe a lead is braking, m/s^2. See the cap
# discussion above — this is a NOISE bound, not a safety bound.
LEAD_DECEL_MAX = 4.0

# Time constant on the belief, seconds. The belief feeds a distance that scales
# as 1/decel, so an unfiltered step in `aLeadK` is a step in the obstacle
# position. Symmetric on purpose: rising fast would jab, and falling fast would
# hand the throttle back the instant a noisy frame said the lead had eased.
LEAD_DECEL_TAU = 0.25


def _finite(x) -> bool:
  return isinstance(x, (int, float)) and x == x and x not in (float('inf'), float('-inf'))


def believed_lead_decel(a_lead, comfort_brake: float) -> float:
  """The deceleration we will credit a lead with, m/s^2.

  Pure. Returns exactly `comfort_brake` for any lead that is not braking harder
  than we would, and for any input we cannot trust — so a dead or garbage
  radar accel degrades to the pre-v3.5.5 number rather than to a guess.
  """
  if not _finite(a_lead) or not _finite(comfort_brake) or comfort_brake <= 0.0:
    return float(comfort_brake) if _finite(comfort_brake) and comfort_brake > 0.0 else 1.0
  decel = -float(a_lead)
  if decel < comfort_brake:
    return float(comfort_brake)
  return float(min(decel, LEAD_DECEL_MAX))


def stopped_equivalence_distance(v_lead, decel: float):
  """How far a lead moving at `v_lead` travels before stopping at `decel`.

  Accepts a scalar or a numpy array for `v_lead` (the MPC passes the whole
  extrapolated velocity trajectory), so this stays plain arithmetic.
  """
  return (v_lead ** 2) / (2.0 * decel)


class LeadDecelBelief:
  """First-order filter on `believed_lead_decel`, one per tracked lead.

  Seeded at — and reset to — `comfort_brake`, so a lead that has just appeared
  is credited with nothing more than the old assumption until it has been
  observed braking for a moment. That ordering matters: a fresh track's first
  `aLeadK` samples are its least reliable.
  """

  def __init__(self, comfort_brake: float, dt: float = 0.05, tau: float = LEAD_DECEL_TAU):
    self.comfort_brake = float(comfort_brake)
    self.dt = float(dt)
    self.tau = float(tau)
    self.x = float(comfort_brake)

  def reset(self) -> None:
    self.x = self.comfort_brake

  def update(self, a_lead, lead_status: bool) -> float:
    if not lead_status:
      self.reset()
      return self.x
    target = believed_lead_decel(a_lead, self.comfort_brake)
    if self.tau <= 0.0:
      self.x = target
      return self.x
    a = self.dt / (self.tau + self.dt)
    self.x += (target - self.x) * a
    # never fall below the old assumption, whatever the filter state
    self.x = max(self.comfort_brake, min(self.x, LEAD_DECEL_MAX))
    return self.x
