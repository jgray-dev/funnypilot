"""FunnyPilot v3.6.2 — "how hard did the car have to work to get round that?"

This is the module that turns the user's definition of the ideal corner speed —
as fast as possible WITHOUT lateral oscillation, WITHOUT the driver-torque
clamp cutting our steering request, and WITHOUT the steering controller hitting
its limits — into a number. So the tests are organised by those three signals,
plus the one property that makes requirement 4 work: it must measure the same
thing with nothing engaged.

Import-light: stdlib only.
"""
import math

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_effort as CE


DT = 0.01     # the carState rate this is actually fed at


def drive(effort, seconds, angle_fn, v=20.0, curv=0.01, torque=0.0,
          lat_active=False, saturated=False, eps_limited=False, pass_=None):
  """Feed `effort` for `seconds`, steering by `angle_fn(t)`."""
  n = int(seconds / DT)
  for i in range(n):
    t = i * DT
    effort.update(DT, v, curv, angle_fn(t), torque, lat_active, saturated, eps_limited)
    if pass_ is not None:
      pass_.add(effort, DT, v)
  return effort


class TestMeasuredLateralAcceleration:
  def test_it_is_v_squared_times_curvature(self):
    e = CE.LateralEffort()
    e.update(DT, 20.0, 0.01, 0.0, 0.0, False)
    assert e.a_lat == pytest.approx(4.0)

  def test_the_sign_of_the_curvature_does_not_matter(self):
    a, b = CE.LateralEffort(), CE.LateralEffort()
    a.update(DT, 20.0, 0.01, 0.0, 0.0, False)
    b.update(DT, 20.0, -0.01, 0.0, 0.0, False)
    assert a.a_lat == pytest.approx(b.a_lat)

  def test_garbage_reads_as_zero_not_as_an_exception(self):
    e = CE.LateralEffort()
    for v, k in ((float('nan'), 0.01), (20.0, float('nan')), (None, None)):
      e.update(DT, v, k, 0.0, 0.0, False)
      assert e.a_lat == e.a_lat and e.a_lat >= 0.0


class TestOscillation:
  """The signal that works in BOTH regimes, which is what lets a corner be
  learned with nothing engaged: a driver sawing at the wheel and a controller
  fighting a bend look the same to a high-pass on the steering angle."""

  def test_a_smooth_turn_in_is_not_oscillation(self):
    """The steady ramp of steering into a bend must be removed by the
    high-pass, or every corner would read as maximum stress."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0 * min(t / 2.0, 1.0), pass_=p)
    _peak, sev = p.verdict()
    assert p.reversals == 0
    assert sev < CE.CLEAN_TH

  def test_sawing_at_the_wheel_is(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    # 2 Hz, +-4 degrees on top of a steady 30 degrees of lock
    drive(e, 4.0, lambda t: 30.0 + 4.0 * math.sin(2 * math.pi * 2.0 * t), pass_=p)
    _peak, sev = p.verdict()
    assert p.reversals >= 8
    assert sev > 1.0

  def test_small_wobble_is_not_counted(self):
    """MUTATION: drop OSC_AMP_DEG. Road camber, sensor noise and ordinary
    lane-keeping all cross zero constantly; without an amplitude qualifier
    every corner on every road is an oscillation."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0 + 0.3 * math.sin(2 * math.pi * 3.0 * t), pass_=p)
    assert p.reversals == 0

  def test_severity_scales_with_how_bad_it_is(self):
    """Not a threshold — a ratio. Requirement 4 needs "how far past", and this
    is where that number comes from."""
    sevs = []
    for hz in (1.0, 2.0, 4.0):
      e, p = CE.LateralEffort(), CE.CornerPass()
      p.begin()
      drive(e, 4.0, lambda t, hz=hz: 30.0 + 4.0 * math.sin(2 * math.pi * hz * t), pass_=p)
      sevs.append(p.verdict()[1])
    assert sevs[0] < sevs[1] < sevs[2]

  def test_severity_is_capped(self):
    """MUTATION: drop MAX_SEVERITY. One pothole mid-bend could otherwise drive
    the learned ceiling to the floor in a single visit — the interval is meant
    to close, not collapse."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0 + 6.0 * math.sin(2 * math.pi * 12.0 * t), pass_=p)
    assert p.verdict()[1] <= CE.MAX_SEVERITY


class TestTheTorqueClamp:
  """`|carState.steeringTorque|` past TBAR_LIMIT means the K5's driver-torque
  clamp is reducing what we may ask the rack for — "TBAR limiting our steering
  torque", by name."""

  def test_high_driver_torque_while_engaged_is_stress(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0, torque=CE.TBAR_LIMIT + 20.0, lat_active=True, pass_=p)
    assert p.verdict()[1] >= 1.0

  def test_the_same_torque_with_lateral_OFF_is_not(self):
    """MUTATION: count the clamp regardless of lat_active. With the driver
    steering, a high reading is the driver driving — every corner they take by
    hand would be condemned, and the map would fill with corners marked
    dangerous because a human turned the wheel."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0, torque=CE.TBAR_LIMIT + 20.0, lat_active=False, pass_=p)
    assert p.verdict()[1] < CE.CLEAN_TH

  def test_torque_under_the_limit_is_not_stress(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0, torque=CE.TBAR_LIMIT - 10.0, lat_active=True, pass_=p)
    assert p.verdict()[1] < CE.CLEAN_TH


class TestSteeringLimits:
  def test_saturation_is_stress(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0, lat_active=True, saturated=True, pass_=p)
    assert p.verdict()[1] >= 1.0

  def test_the_eps_clamp_biting_is_stress(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0, lat_active=True, eps_limited=True, pass_=p)
    assert p.verdict()[1] >= 1.0

  def test_a_brief_touch_of_the_limit_is_not(self):
    """LIMIT_FRAC_LIMIT is a fraction of the pass, not an event count: one
    frame of saturation on a bumpy corner is not the corner being too fast."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    n = int(4.0 / DT)
    for i in range(n):
      e.update(DT, 20.0, 0.01, 30.0, 0.0, True, saturated=(i < 3))
      p.add(e, DT, 20.0)
    assert p.verdict()[1] < 1.0

  def test_the_two_components_are_compared_not_summed(self):
    """MUTATION: add the oscillation and limit terms. Two half-breaches would
    then manufacture a full one, and a corner that was merely lively in two
    different ways would be condemned."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    n = int(6.0 / DT)
    for i in range(n):
      t = i * DT
      # ~half the oscillation threshold AND ~half the limit-time threshold
      e.update(DT, 20.0, 0.01, 30.0 + 4.0 * math.sin(2 * math.pi * 0.8 * t), 0.0,
               True, saturated=(i % 20 == 0))
      p.add(e, DT, 20.0)
    assert p.verdict()[1] < 1.0


class TestPassValidity:
  def test_a_short_pass_is_unusable(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, CE.MIN_PASS_S / 2, lambda t: 30.0, pass_=p)
    assert not p.usable()

  def test_a_crawl_is_unusable(self):
    """Below MIN_PASS_V the lateral signals say nothing: a car at walking pace
    can be steered to any angle with no effort at all."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0, v=2.0, pass_=p)
    assert not p.usable()

  def test_a_blocked_pass_stays_blocked(self):
    """One blocked frame condemns the whole pass — a lane change or a
    standstill anywhere in a bend makes the whole traversal unrepresentative,
    and there is no honest way to use the rest of it."""
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    p.add(e, DT, 20.0, blocked=True)
    drive(e, 4.0, lambda t: 30.0, pass_=p)
    assert not p.usable()

  def test_a_normal_pass_is_usable(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    drive(e, 4.0, lambda t: 30.0 * min(t / 2.0, 1.0), pass_=p)
    assert p.usable()

  def test_the_peak_is_the_peak(self):
    e, p = CE.LateralEffort(), CE.CornerPass()
    p.begin()
    for k in (0.005, 0.02, 0.008):
      for _ in range(50):
        e.update(DT, 20.0, k, 30.0, 0.0, False)
        p.add(e, DT, 20.0)
    assert p.verdict()[0] == pytest.approx(400.0 * 0.02)

  def test_an_empty_pass_reports_nothing(self):
    assert CE.CornerPass().verdict() == (0.0, 0.0)
