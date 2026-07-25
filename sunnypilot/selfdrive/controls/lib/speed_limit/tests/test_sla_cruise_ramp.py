"""FunnyPilot v3.4.0 — SLA predictive SET-SPEED ramp.

The v3.3.9 attempt shaped an internal MPC cap and did nothing under DEC.
This ramp instead walks the real cruise set speed (what cruise_ext writes and
the cluster displays), which every MPC mode honors identically.

Covered here: the envelope shape down into a slower zone, the up-ramp into a
faster one, slew bounding, and — most importantly — that the ramp's own
set-speed writes do NOT re-derive (and thus destroy) the driver's carried
offset ratio, which is the subtle way this feature could silently break.

Reuses the existing import-light harness from test_speed_limit_assist.py.
"""
from openpilot.common.constants import CV
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import (
  RAMP_DECEL, RAMP_UP_DIST, RAMP_MAX_RATE,
)
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.tests.test_speed_limit_assist import (
  FakeEvents, make_sla, step, activate,
)

MPH = CV.MPH_TO_MS
DT = 0.05  # planner rate


def approach(sla, events, cluster_mph, limit_mph, next_limit_mph, dist_m, n=1):
  step(sla, events, cluster_mph=cluster_mph, limit_mph=limit_mph,
       next_limit_mph=next_limit_mph, next_dist=dist_m, n=n)


class TestCruiseRamp:
  def test_inactive_requests_nothing(self):
    sla = make_sla()
    events = FakeEvents()
    step(sla, events, cluster_mph=50., limit_mph=45.)
    assert sla.v_cruise_target == 0.

  def test_steady_zone_targets_current_zone_speed(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=50., limit_mph=45.)  # ~+11% carried
    step(sla, events, cluster_mph=50., limit_mph=45., n=5)
    assert abs(sla.v_cruise_target - sla.effective_speed_limit_target) < 0.5

  def test_no_next_zone_holds_current_target(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=45., limit_mph=45.)
    step(sla, events, cluster_mph=45., limit_mph=45., n=10)
    assert abs(sla.v_cruise_target - 45. * MPH) < 0.5

  def test_walks_set_speed_down_before_slower_zone(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    v = 65. * MPH
    nxt = 30. * MPH
    d = ((v ** 2 - nxt ** 2) / (2 * RAMP_DECEL)) + 30.
    last = None
    while d > 0:
      approach(sla, events, 65., 65., 30., d)
      if last is not None:
        assert sla.v_cruise_target <= last + 1e-6  # monotonically walking down
      last = sla.v_cruise_target
      d -= v * DT
    assert sla.v_cruise_target < v          # it actually moved
    assert sla.v_cruise_target < 35. * MPH  # and landed near the new zone

  def test_never_exceeds_current_zone_target_while_slowing(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=55., limit_mph=55.)
    for d in (400., 300., 200., 100., 50., 10.):
      approach(sla, events, 55., 55., 25., d, n=3)
      assert sla.v_cruise_target <= 55. * MPH + 1e-6

  def test_walks_set_speed_up_into_faster_zone(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=35., limit_mph=35.)
    # far out: no anticipation yet
    approach(sla, events, 35., 35., 65., RAMP_UP_DIST * 3, n=3)
    assert abs(sla.v_cruise_target - 35. * MPH) < 0.5
    # closing in: the set speed rises toward the new zone
    prev = sla.v_cruise_target
    for d in (RAMP_UP_DIST * 0.75, RAMP_UP_DIST * 0.5, RAMP_UP_DIST * 0.25, 1.0):
      approach(sla, events, 35., 35., 65., d, n=8)
      assert sla.v_cruise_target >= prev - 1e-6
      prev = sla.v_cruise_target
    assert sla.v_cruise_target > 35. * MPH

  def test_up_ramp_never_overshoots_next_zone(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=35., limit_mph=35.)
    for d in (80., 40., 10., 0.5):
      approach(sla, events, 35., 35., 65., d, n=8)
      assert sla.v_cruise_target <= 65. * MPH + 1e-6

  def test_set_speed_never_jumps(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=65., limit_mph=65.)
    step(sla, events, cluster_mph=65., limit_mph=65., n=2)
    prev = sla.v_cruise_target
    # adversarial: a much slower zone appears abruptly at close range
    for _ in range(20):
      approach(sla, events, 65., 65., 15., 5.)
      assert abs(sla.v_cruise_target - prev) <= RAMP_MAX_RATE * DT + 1e-6
      prev = sla.v_cruise_target


class TestRatioPreservedDuringRamp:
  """The subtle failure this guard exists for: mid-approach the cluster sits
  BETWEEN zones, so re-deriving the ratio from it would silently wipe the
  driver's carried offset."""

  def test_ramp_command_does_not_rederive_ratio(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=60., limit_mph=50.)  # +20% carried
    ratio0 = sla.dynamic_offset_ratio
    assert abs(ratio0 - 0.2) < 0.03

    # simulate cruise_ext following the ramp: the cluster tracks v_cruise_target
    d = 400.
    for _ in range(80):
      cluster_mph = (sla.v_cruise_target / MPH) if sla.v_cruise_target > 0 else 60.
      approach(sla, events, cluster_mph, 50., 25., d)
      d = max(0., d - sla.v_ego * DT)
    assert abs(sla.dynamic_offset_ratio - ratio0) < 0.03, "ramp corrupted the carried offset ratio"

  def test_real_button_press_still_rederives_ratio(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=50., limit_mph=45.)
    ratio0 = sla.dynamic_offset_ratio
    # driver bumps the set speed well clear of anything the ramp asked for
    step(sla, events, cluster_mph=60., limit_mph=45., n=3)
    assert sla.dynamic_offset_ratio > ratio0 + 0.05, "driver adjustment must still set the ratio"

  def test_boundary_snap_remains_idempotent(self):
    sla = make_sla()
    events = FakeEvents()
    activate(sla, events, cluster_mph=60., limit_mph=50.)  # +20%
    ratio0 = sla.dynamic_offset_ratio
    # zone changes; cruise_ext snaps the cluster to new_limit * (1 + ratio)
    new_limit_mph = 25.
    step(sla, events, cluster_mph=new_limit_mph * (1.0 + ratio0), limit_mph=new_limit_mph, n=3)
    assert abs(sla.dynamic_offset_ratio - ratio0) < 0.03
