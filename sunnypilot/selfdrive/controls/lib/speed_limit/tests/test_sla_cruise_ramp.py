"""FunnyPilot v3.4.5 — SLA predictive SET-SPEED ramp.

WHAT WENT WRONG BEFORE, and why several of these tests look paranoid: through
v3.4.4 this feature never executed on a moving car. speed_limit_resolver.py
computed `distance_to_next_limit` as `monotonic - unix_epoch`, producing ~4e10 m,
so `d > 0` was always true but every distance comparison was meaningless and the
envelope always evaluated far above the current target. The old suite passed
throughout because it fed `next_dist` directly and never exercised the resolver.
Lesson taken here: a test that supplies the value under suspicion cannot detect
a bug in how that value is produced — hence test_speed_limit_resolver_clock.py
next door, which tests the producer.

Each test below is written to FAIL if its specific bug is reintroduced. The
mutation each one guards is named in its docstring.

Import-light; reuses the harness from test_speed_limit_assist.py.
"""
import math

from cereal import car
from openpilot.common.constants import CV
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit import speed_limit_assist as sla_mod
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import (
  RATE_NOM, RATE_MAX, RAMP_T_MAX, RAMP_D_MAX, CONFIRM_N,
  RAMP_UP_T, RAMP_UP_T_MAX, RAMP_UP_D_MIN, GATE_V_MARGIN, GATE_MAX_FRAMES,
  LATCH_RELEASE_D, RAMP_ARRIVE_EARLY_T, MIN_SET_SPEED_KPH_METRIC, MIN_SET_SPEED_KPH_IMPERIAL,
)
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.tests.test_speed_limit_assist import (
  FakeEvents, make_sla, step, activate,
)

ButtonType = car.CarState.ButtonEvent.Type
MPH = CV.MPH_TO_MS
DT = 0.05  # planner rate


def approach(sla, events, cluster_mph, limit_mph, next_limit_mph, dist_m, n=1):
  step(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph,
       next_limit_mph=next_limit_mph, next_dist=dist_m, n=n)


def up_window(v_ego_mph, cur_mph, next_mph):
  """v3.4.8 up-ramp window in metres — a travel time, so it tracks v_ego."""
  dv = (next_mph - cur_mph) * MPH
  t = min(RAMP_UP_T_MAX, max(RAMP_UP_T, dv / RATE_NOM))
  return max(RAMP_UP_D_MIN, v_ego_mph * MPH * t)


def settle(sla, events, cluster_mph, limit_mph):
  """Run past the button-intent window so re-seeding stops and the ramp is live."""
  step(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph,
       n=sla_mod.BUTTON_INTENT_FRAMES + 2)


def press(sla, button_type=ButtonType.decelCruise):
  CS = car.CarState.new_message()
  be = CS.init('buttonEvents', 1)
  be[0].type = button_type
  be[0].pressed = False
  sla.update_car_state(CS)


class TestRateDerivation:
  """The constants are derived, not chosen; these pin the derivations."""

  def test_nominal_rate_is_about_one_mph_per_second(self):
    # The user's literal request: "roughly 1mph change every 1 second".
    assert abs(RATE_NOM - 1.0 * MPH) < 0.05

  def test_max_rate_cannot_exceed_mpc_cruise_authority(self):
    """MUTATION: raise RATE_MAX above 1.2.

    long_mpc.py's CRUISE_MIN_ACCEL bounds how hard the cruise obstacle can ask
    the car to decelerate. A set speed slewing faster than that moves a number
    the car provably cannot follow -- it would LOOK like the system responded
    while nothing happened.
    """
    import re
    import pathlib
    # .../<repo>/sunnypilot/selfdrive/controls/lib/speed_limit/ -> parents[4] is
    # the `sunnypilot` package dir, whose parent is the repo root.
    repo = pathlib.Path(sla_mod.__file__).parents[5]
    src = repo / "selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py"
    m = re.search(r"^CRUISE_MIN_ACCEL\s*=\s*(-?[\d.]+)", src.read_text(), re.M)
    assert m, "CRUISE_MIN_ACCEL not found -- this guard has gone stale, fix it"
    assert RATE_MAX <= abs(float(m.group(1))) + 1e-9

  def test_design_case_engages_within_the_requested_window(self):
    """70 -> 45 mph: v3.4.7 widened the runway to ~400 m of envelope / 15-20 s.

    d_engage here is ENVELOPE distance (d_eff). The ramp starts
    RAMP_ARRIVE_EARLY_T * v_ego earlier than this and finishes that far before
    the sign -- see test_finishes_the_descent_before_the_boundary.
    """
    v0, v1 = 70. * MPH, 45. * MPH
    a = min(max(max((v0 - v1) / RAMP_T_MAX, (v0 ** 2 - v1 ** 2) / (2 * RAMP_D_MAX)), RATE_NOM), RATE_MAX)
    d_engage = (v0 ** 2 - v1 ** 2) / (2 * a)
    assert 380. <= d_engage <= 420.
    assert 12.0 <= (v0 - v1) / a <= 20.0

  def test_finishes_the_descent_before_the_boundary(self):
    """MUTATION: shrink RAMP_ARRIVE_EARLY_T back toward zero.

    The v3.4.6 complaint was arriving at the sign still slowing down. The set
    speed is a request the car trails, so the envelope has to bottom out with
    real distance in hand, not exactly at d = 0.
    """
    v0 = 70. * MPH
    assert RAMP_ARRIVE_EARLY_T * v0 > 75., "too little room for the car to settle onto the set speed"

  def test_gentle_change_uses_the_nominal_rate(self):
    v0, v1 = 45. * MPH, 35. * MPH
    a = min(max(max((v0 - v1) / RAMP_T_MAX, (v0 ** 2 - v1 ** 2) / (2 * RAMP_D_MAX)), RATE_NOM), RATE_MAX)
    assert abs(a - RATE_NOM) < 1e-9


class TestDeletedInferences:
  """MUTATION: reintroduce any of the removed coast/slew constants."""

  def test_removed_symbols_stay_removed(self):
    for name in ("RAMP_DECEL", "RAMP_MAX_RATE", "GATE_COAST_ACCEL",
                 "GATE_TIME_BUFFER", "GATE_MIN_OVER"):
      assert not hasattr(sla_mod, name), f"{name} was deleted in v3.4.5; do not bring it back"

  def test_cluster_change_heuristic_stays_removed(self):
    # Replaced by the button discriminator. Recognising our own commanded value
    # fails exactly when a driver press lands on a value the ramp passed through.
    assert not hasattr(sla_mod.SpeedLimitAssist, "_cluster_change_is_ours")


class TestCruiseRamp:
  def test_inactive_requests_nothing(self):
    sla = make_sla()
    events = FakeEvents()
    step(sla, events, cluster_mph=50., limit_mph=45.)
    assert sla.v_cruise_target == 0.

  def test_active_with_no_next_limit_holds_current_target(self):
    """MUTATION: return 0 instead of current_target when nothing is upcoming.

    0 means "no request" to cruise_ext, which would make the set speed freeze
    wherever it happened to be rather than track the zone.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=45., limit_mph=45.)
    settle(sla, events, 45., 45.)
    assert sla.v_cruise_target > 0.
    assert abs(sla.v_cruise_target - 45. * MPH) < 0.5

  def test_engage_distance_for_the_design_case(self):
    """MUTATION: drop the distance bound, or the confirmation gate.

    Far out the target must still be the CURRENT zone; inside the envelope it
    must have started moving. v3.4.7: the boundary sits at RAMP_D_MAX plus the
    early-arrival margin, i.e. 400 + 3 * 31.3 = ~494 m at 70 mph.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=70., limit_mph=70.)
    settle(sla, events, 70., 70.)

    approach(sla, events, 70., 70., 45., 650., n=CONFIRM_N + 2)
    assert abs(sla.v_cruise_target - 70. * MPH) < 0.2, "must not anticipate beyond RAMP_D_MAX"

    approach(sla, events, 70., 70., 45., 450., n=CONFIRM_N + 2)
    assert sla.v_cruise_target < 70. * MPH - 0.05, "must be walking down by 450 m"

  def test_single_frame_ghost_limit_never_moves_the_set_speed(self):
    """MUTATION: delete the CONFIRM_N gate.

    OSM/mapd can emit a one-frame bogus upcoming limit when the route match
    jumps. Acting on it would yank the driver's set speed for no reason.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=70., limit_mph=70.)
    settle(sla, events, 70., 70.)
    before = sla.v_cruise_target
    approach(sla, events, 70., 70., 25., 60., n=1)  # one frame only
    assert abs(sla.v_cruise_target - before) < 1e-9

  def test_descent_is_monotone_under_distance_noise(self):
    """MUTATION: delete the latch.

    OSM distance is great-circle and ticks back up on noise; without the latch
    the displayed set speed visibly bounces mid-approach.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    d = 250.
    last = None
    for i in range(120):
      noisy = d + (20. if i % 5 == 0 else 0.)  # +20 m jitter, below LATCH_RELEASE_D
      approach(sla, events, 65., 65., 30., max(1., noisy))
      if last is not None:
        assert sla.v_cruise_target <= last + 1e-6, f"set speed bounced up at frame {i}"
      last = sla.v_cruise_target
      d = max(1., d - 65. * MPH * DT)
    assert sla.v_cruise_target < 65. * MPH

  def test_sustained_distance_jump_releases_the_latch(self):
    """MUTATION: latch without a release.

    A turn onto a different road legitimately moves the next zone further away.
    A permanent latch would hold the set speed down for the rest of the drive.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    for d in (200., 150., 100.):
      approach(sla, events, 65., 65., 30., d, n=CONFIRM_N + 2)
    latched = sla.v_cruise_target
    assert latched < 65. * MPH
    # Route change: the next zone is now far enough out that the envelope no
    # longer binds at all, so the correct answer is "go back to the current
    # zone's target". The distance has to be THIS large to be a real test: a
    # merely LATCH_RELEASE_D-sized jump still leaves the envelope below the
    # current set speed, so the ramp keeps descending either way and the
    # assertion would pass with the release deleted.
    far = 100. + LATCH_RELEASE_D * 2
    assert far > 100. + LATCH_RELEASE_D  # sustained enough to count as a route change
    approach(sla, events, 65., 65., 30., 2000., n=200)
    assert sla.v_cruise_target > latched + 0.5, "latch never released"

  def test_arrive_early_scales_with_v_ego(self):
    """MUTATION: scale RAMP_ARRIVE_EARLY_T by next_target (the v3.4.0 bug).

    The early-arrival margin is a TRAVEL TIME, so it must depend on how fast the
    boundary is approaching, not on the destination speed.
    """
    assert RAMP_ARRIVE_EARLY_T > 0.

    def target_at(v_ego_mph):
      sla = make_sla()
      events = FakeEvents()
      activate(sla, events, cluster_mph=60., limit_mph=60.)
      settle(sla, events, 60., 60.)
      # Long enough for the RATE_MAX slew cap to stop binding and the set speed
      # to SETTLE on the envelope: with only a handful of frames both cases sit
      # at the same slew-limited value and the test is vacuous.
      step(sla, events, cluster_mph=60., limit_mph=60., v_ego_mph=v_ego_mph,
           next_limit_mph=30., next_dist=150., n=200)
      return sla.v_cruise_target

    # Same geometry, same zones; only v_ego differs. A larger v_ego consumes
    # more d_eff, so the commanded set speed must be lower.
    assert target_at(70.) < target_at(20.) - 1e-6

  def test_walks_set_speed_up_into_faster_zone(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=35., limit_mph=35.)
    settle(sla, events, 35., 35.)
    w = up_window(35., 35., 65.)
    approach(sla, events, 35., 35., 65., w * 3, n=CONFIRM_N + 2)
    assert abs(sla.v_cruise_target - 35. * MPH) < 0.2
    prev = sla.v_cruise_target
    for d in (w * 0.75, w * 0.5, w * 0.25, 1.0):
      approach(sla, events, 35., 35., 65., d, n=12)
      assert sla.v_cruise_target >= prev - 1e-6
      prev = sla.v_cruise_target
    assert sla.v_cruise_target > 35. * MPH

  def test_up_ramp_never_overshoots_next_zone(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=35., limit_mph=35.)
    settle(sla, events, 35., 35.)
    for d in (80., 40., 10., 0.5):
      approach(sla, events, 35., 35., 65., d, n=12)
      assert sla.v_cruise_target <= 65. * MPH + 1e-6

  def test_set_speed_never_slews_faster_than_rate_max(self):
    """MUTATION: skip the slew cap on the first engaged frame (the v3.4.0
    `if self.v_cruise_target > 0.` guard did exactly this)."""
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    prev = sla.v_cruise_target
    for _ in range(40):
      approach(sla, events, 65., 65., 15., 5.)  # adversarial: slow zone right there
      assert abs(sla.v_cruise_target - prev) <= RATE_MAX * DT + 1e-6
      prev = sla.v_cruise_target

  def test_zone_change_reseeds_and_bypasses_slew(self):
    """MUTATION: remove the re-seed branch.

    At the boundary current_target steps to the new zone. Without a re-seed the
    ramp would crawl there at RATE_MAX, i.e. exactly the "spams control commands
    one mph at a time after entering the zone" behaviour this version fixes.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=60., limit_mph=60.)
    settle(sla, events, 60., 60.)
    step(sla, events, cluster_mph=60., limit_mph=25., n=1)  # boundary crossed
    assert abs(sla.v_cruise_target - 25. * MPH) < 0.5

  def test_never_commands_below_the_minimum_set_speed(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=20., limit_mph=20.)
    settle(sla, events, 20., 20.)
    approach(sla, events, 20., 20., 5., 20., n=200)
    assert sla.v_cruise_target >= MIN_SET_SPEED_KPH_IMPERIAL * CV.KPH_TO_MS - 1e-6

  def test_min_set_speed_matches_the_real_helper(self):
    """MUTATION: change either constant. These are duplicated to keep this
    module import-light, so they must be checked against the source of truth."""
    from openpilot.sunnypilot.selfdrive.car.intelligent_cruise_button_management.helpers import (
      get_minimum_set_speed,
    )
    assert MIN_SET_SPEED_KPH_METRIC == get_minimum_set_speed(True)
    assert MIN_SET_SPEED_KPH_IMPERIAL == get_minimum_set_speed(False)


class TestRatioPreserved:
  """The ratio is the one piece of state that persists across zones. Corrupting
  it is silent -- the car just quietly stops carrying the driver's offset."""

  def test_ramp_does_not_rederive_ratio(self):
    """MUTATION: re-derive on any cluster change (i.e. drop the button gate).

    Mid-approach the cluster sits BETWEEN zones, so re-deriving there collapses
    a carried +20% to whatever the ramp is passing through.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=60., limit_mph=50.)  # +20% carried
    ratio0 = sla.dynamic_offset_ratio
    assert abs(ratio0 - 0.2) < 0.03
    settle(sla, events, 60., 50.)

    d = 400.
    for _ in range(200):  # cruise_ext follows the ramp; the cluster tracks it
      cluster_mph = (sla.v_cruise_target / MPH) if sla.v_cruise_target > 0 else 60.
      approach(sla, events, cluster_mph, 50., 25., d)
      d = max(1., d - sla.v_ego * DT)
    assert abs(sla.dynamic_offset_ratio - ratio0) < 1e-9, "ramp corrupted the carried offset"

  def test_button_press_rederives_exactly_once(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=50., limit_mph=45.)
    settle(sla, events, 50., 45.)
    ratio0 = sla.dynamic_offset_ratio

    press(sla, ButtonType.accelCruise)
    step(sla, events, cluster_mph=60., limit_mph=45., n=1)
    ratio1 = sla.dynamic_offset_ratio
    assert ratio1 > ratio0 + 0.05, "driver adjustment must set the ratio"

    # ...and the ramp moving the cluster afterwards must not move it again
    settle(sla, events, 60., 45.)
    for _ in range(50):
      cluster_mph = (sla.v_cruise_target / MPH) if sla.v_cruise_target > 0 else 60.
      approach(sla, events, cluster_mph, 45., 25., 200.)
    assert abs(sla.dynamic_offset_ratio - ratio1) < 1e-9

  def test_button_press_mid_descent_is_a_delta_not_an_absolute(self):
    """v3.4.9. MUTATION: re-derive the ratio absolutely from the cluster while
    the ramp is displacing it (the v3.4.5 behaviour), or fail to shift the
    monotone latch by the driver's own delta.

    Mid-descent the cluster reads whatever the RAMP walked it to, so an absolute
    re-derivation reads OUR displacement as THEIR offset: +30% carried into a 50
    zone, walked down to 40 mph on the way to a 30 zone, one tap of `+` and the
    old code stored (41 - 50)/50 = -18%. That is the reported "it forgets where
    SLA was set before, and the new zone is all buggy" — the 30 zone would then
    be entered at 24.6 mph instead of 39.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=50.)  # +30% carried
    ratio0 = sla.dynamic_offset_ratio
    settle(sla, events, 65., 50.)
    for d in (200., 150., 120.):
      approach(sla, events, sla.v_cruise_target / MPH, 50., 30., d, n=CONFIRM_N + 2)
    target_before = sla.v_cruise_target
    latch_before = sla._latch
    assert target_before < 65. * MPH, "ramp must be descending for this test to mean anything"
    assert latch_before > 0.

    driver_mph = target_before / MPH + 3.
    press(sla, ButtonType.accelCruise)
    approach(sla, events, driver_mph, 50., 30., 120., n=1)

    # the driver's value is ADOPTED, not overwritten by the zone target
    assert abs(sla.v_cruise_target - driver_mph * MPH) < 0.5
    # ...and read as a DELTA: the carried offset goes UP, never negative
    assert sla.dynamic_offset_ratio > ratio0
    assert sla.dynamic_offset_ratio > 0.
    # ...and the latch moves with them, so it cannot claw the raise back down
    assert sla._latch > latch_before + 1.0

  def test_press_mid_descent_lands_the_new_zone_on_the_carried_offset(self):
    """v3.4.9. The end-to-end version of the bug: what the driver actually sees
    is not the ratio, it is where the set speed ends up after the boundary."""
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=50.)  # +30%
    settle(sla, events, 65., 50.)
    for d in (200., 150., 120.):
      approach(sla, events, sla.v_cruise_target / MPH, 50., 30., d, n=CONFIRM_N + 2)

    driver_mph = sla.v_cruise_target / MPH + 2.
    press(sla, ButtonType.accelCruise)
    approach(sla, events, driver_mph, 50., 30., 120., n=1)
    ratio = sla.dynamic_offset_ratio

    # cross into the 30 zone
    step(sla, events, cluster_mph=driver_mph, limit_mph=30., n=1)
    assert abs(sla.v_cruise_target - 30. * MPH * (1. + ratio)) < 0.5
    # the whole point: still comfortably ABOVE the posted limit, as carried
    assert sla.v_cruise_target > 30. * MPH


class TestUpRampReachesLongitudinalControl:
  """v3.4.9. The up-ramp moved the cluster and nothing else. `output_v_target`
  is SLA's entry in the speed governor's min(), and it returned the CURRENT
  zone's target, so SLA itself pinned the car to the old limit for the whole
  approach and the rise arrived as one step at the boundary."""

  def test_published_target_follows_the_up_ramp(self):
    """MUTATION: return `effective_speed_limit_target` from
    get_v_target_from_control."""
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=45., limit_mph=45.)
    settle(sla, events, 45., 45.)
    zone_target = sla.effective_speed_limit_target

    d = up_window(45., 45., 60.)
    seen_above = False
    for _ in range(120):
      approach(sla, events, sla.v_cruise_target / MPH, 45., 60., d, n=1)
      d = max(1., d - sla.v_ego * DT)
      if sla.output_v_target > zone_target + 0.3:
        seen_above = True
    assert seen_above, "long control never saw the rise before the boundary"
    assert abs(sla.output_v_target - sla.v_cruise_target) < 1e-9

  def test_published_target_follows_the_down_ramp_unchanged(self):
    """The descent must be bit-identical: there the ramp target IS the more
    restrictive of the two, which is why the bug only ever showed going up."""
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    for d in (300., 250., 200., 150.):
      approach(sla, events, sla.v_cruise_target / MPH, 65., 30., d, n=CONFIRM_N + 2)
    assert sla.output_v_target < sla.effective_speed_limit_target
    assert abs(sla.output_v_target - sla.v_cruise_target) < 1e-9

  def test_ratio_survives_five_zones_with_quantization(self):
    """MUTATION: any re-derivation drift. Rounding the set speed to the display
    grid must not walk the ratio, or a long drive slowly erases the offset."""
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=60., limit_mph=50.)
    ratio0 = sla.dynamic_offset_ratio
    settle(sla, events, 60., 50.)
    for limit in (35., 55., 25., 45., 65.):
      # cruise_ext quantizes to whole mph before writing the cluster
      cluster = round(limit * (1.0 + ratio0))
      step(sla, events, cluster_mph=cluster, limit_mph=limit, n=20)
    assert abs(sla.dynamic_offset_ratio - ratio0) < 1e-9


class TestGateNeverStrandsTheCar:
  """v3.4.8 regression guards for the reported highway coast.

  Field report: entered a new zone below target, the set speed snapped up, and
  then the car refused to accelerate for ~10 s -- the pedal did not help and
  only cycling SLA cleared it. The gate was pure set-speed geometry, so it fired
  while the car was already SLOWER than the target it was supposedly protecting.
  """

  def test_gate_off_when_already_below_the_ramp_target(self):
    """MUTATION: drop the v_ego term from _update_gas_gate.

    Same geometry as test_gate_follows_the_ramp -- the ramp IS holding the set
    speed down -- but the car is well below the ramp's own target. There is no
    throttle to take away, and taking it away is what stranded the car.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    step(sla, events, cluster_mph=65., limit_mph=65., v_ego_mph=25.,
         next_limit_mph=30., next_dist=200., n=CONFIRM_N + 6)
    assert sla.v_cruise_target < sla.effective_speed_limit_target - 0.1, "ramp must be holding down"
    assert sla.v_ego < sla.v_cruise_target, "precondition: car is slower than the target"
    assert not sla.gas_gate_active

  def test_gate_releases_after_the_watchdog(self):
    """MUTATION: remove GATE_MAX_FRAMES.

    No legitimate descent holds throttle off for 30 s. If one ever does, the
    throttle goes back rather than the car coasting indefinitely.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    approach(sla, events, 65., 65., 30., 200., n=60)
    assert sla.gas_gate_active
    approach(sla, events, 65., 65., 30., 200., n=GATE_MAX_FRAMES + 5)
    assert not sla.gas_gate_active

  def test_gate_margin_is_nonzero(self):
    assert GATE_V_MARGIN > 0.


class TestEngagementSurvivesDropouts:
  def test_single_dropped_frame_does_not_walk_the_set_speed_backwards(self):
    """MUTATION: remove the ENGAGE_GRACE dead reckoning.

    The reported flicker. mapd publishes at 1 Hz and the route match can blink;
    without the grace, _confirm_n resets and the ramp falls through to
    `target = current_target`, driving the set speed the WRONG way for several
    frames before it recovers.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    approach(sla, events, 65., 65., 35., 250., n=CONFIRM_N + 10)
    before = sla.v_cruise_target
    assert before < 65. * MPH - 0.1, "precondition: descent is under way"

    # one frame with the upcoming zone missing, then it comes back
    step(sla, events, cluster_mph=65., limit_mph=65., next_limit_mph=0., next_dist=0., n=1)
    assert sla.v_cruise_target <= before + 1e-6, "set speed jumped back up on a blink"


class TestGasGate:
  def test_gate_is_off_when_the_ramp_is_not_holding_speed_down(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=45., limit_mph=45.)
    settle(sla, events, 45., 45.)
    assert abs(sla.v_cruise_target - sla.effective_speed_limit_target) < 0.2
    assert not sla.gas_gate_active

  def test_gate_follows_the_ramp(self):
    """v3.4.8: run to where the car has genuinely fallen behind the target.

    The gate now needs BOTH conditions, so the first ~1 s of a descent (set
    speed just below v_ego, no throttle being added anyway) no longer gates.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    approach(sla, events, 65., 65., 30., 200., n=60)
    assert sla.v_cruise_target < sla.effective_speed_limit_target - 0.1
    assert sla.v_ego > sla.v_cruise_target + GATE_V_MARGIN
    assert sla.gas_gate_active

  def test_gate_reads_this_frames_ramp_not_last_frames(self):
    """MUTATION: swap the call order back (gate before ramp) in update().

    Driven through the real update() so the ordering is what is under test.
    On the frame the ramp first pulls the target below the zone AND the car is
    above it, the gate must already be true -- no one-frame lag either way.
    """
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    settle(sla, events, 65., 65.)
    saw_gate = False
    for _ in range(120):
      approach(sla, events, 65., 65., 30., 200.)
      expect = (sla.v_cruise_target < sla.effective_speed_limit_target - 0.1
                and sla.v_ego > sla.v_cruise_target + GATE_V_MARGIN)
      assert sla.gas_gate_active == expect
      saw_gate = saw_gate or sla.gas_gate_active
    assert saw_gate, "anti-vacuous: the gate must actually have engaged"

  def test_gate_off_when_inactive(self):
    sla = make_sla()
    events = FakeEvents()
    step(sla, events, cluster_mph=65., limit_mph=65., next_limit_mph=30., next_dist=100., n=5)
    assert not sla.gas_gate_active


class TestEnvelopeMath:
  """Pure-math checks on the closed form, independent of the class."""

  def test_envelope_reaches_next_target_at_the_boundary(self):
    v1 = 30. * MPH
    for a in (RATE_NOM, RATE_MAX):
      assert abs(math.sqrt(v1 ** 2 + 2 * a * 0.) - v1) < 1e-9

  def test_envelope_is_increasing_in_distance(self):
    v1, a = 30. * MPH, RATE_NOM
    vals = [math.sqrt(v1 ** 2 + 2 * a * d) for d in (0., 25., 50., 100.)]
    assert vals == sorted(vals)
