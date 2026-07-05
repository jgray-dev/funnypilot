"""FunnyPilot v3.2.6e — shared curve speed-cap smoothing for SCC-V / SCC-M.

Both curve controllers produce a raw speed cap each frame; this class turns
that into a stable, comfortable governor input:
  * debounced activation (2 consecutive constraining frames) so single-frame
    model/map noise can't grab the throttle,
  * on activation the cap is SEEDED at the current speed and eases down to
    the raw target (no step),
  * fast-tracking downward (EMA), rate-limited release upward so the cap
    doesn't vanish mid-corner-exit,
  * clean deactivation once the cap has released back to the cruise target.

Speed-domain only: the cap feeds the SpeedGovernor min(); the MPC + shaper
own how the car actually slows.
"""

ACTIVATE_MARGIN = 1.0    # m/s below v_cruise the raw cap must dip to activate
ACTIVATE_FRAMES = 2      # consecutive frames before latching
RELEASE_DONE_MARGIN = 0.5  # m/s from v_cruise where the cap is considered released
DOWN_ALPHA = 0.35        # per-frame EMA toward a lower raw cap (fast, ~0.15 s)
RELEASE_RATE = 2.5       # m/s per second upward release of the cap

CAP_INACTIVE = 999.0


class CurveSpeedCap:
  def __init__(self, dt: float):
    self.dt = dt
    self.reset()

  def reset(self) -> None:
    self.value = CAP_INACTIVE
    self.active = False
    self.releasing = False
    self._arm = 0

  def update(self, raw: float, v_ego: float, v_cruise: float) -> float:
    constraining = raw < v_cruise - ACTIVATE_MARGIN

    if not self.active:
      self._arm = self._arm + 1 if constraining else 0
      if self._arm >= ACTIVATE_FRAMES:
        self.active = True
        self.releasing = False
        # seed at the current speed so the cap eases down instead of stepping
        self.value = min(v_cruise, max(v_ego, raw))
        return self.value
      self.value = CAP_INACTIVE
      return self.value

    if raw < self.value:
      self.value += (raw - self.value) * DOWN_ALPHA
      self.releasing = False
    else:
      self.value = min(self.value + RELEASE_RATE * self.dt, max(raw, v_cruise), v_cruise + 1.0)
      self.releasing = not constraining

    if not constraining and self.value >= v_cruise - RELEASE_DONE_MARGIN:
      self.reset()

    return self.value
