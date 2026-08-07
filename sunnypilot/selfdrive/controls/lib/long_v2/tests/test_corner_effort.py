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


class TestTheDisturbanceGate:
  """FunnyPilot v3.6.2 — telling "too fast for this bend" from "the road hit us".

  THE CONFOUND, and it is measured rather than hypothetical: v3.3.8 recorded
  sawing at a railroad crossing with the EPS governor pinned at 100% authority
  and nothing wrong with the speed. In the steering trace that is
  indistinguishable from a corner taken too quickly, so without a guard one
  bumpy crossing inside a bend teaches the store that a good corner is slow --
  permanently, and invisibly, because a too-slow corner produces no symptom
  anyone can see.

  Everything here is exact. The pass is a pair of running sums, so given the
  frames that go in, the severity that comes out is fully determined; the
  helper places an exact COUNT of reversals rather than a rate, because a rate
  at 100 Hz quantises (3.2/s wants a flag every 31.25 frames) and a test whose
  expected value is 6% off the number in its own name is not a pinned value.
  """
  DT = 0.01          # 100 Hz, the rate observe_frame actually runs at

  def _frames(self, pass_, effort, seconds, pitch=0.0, reversals=0,
              limited=False, v=20.0, a_lat=2.0):
    """Drive `seconds` of pass containing EXACTLY `reversals` corrections.

    The reversal flag is set directly rather than synthesised from a steering
    waveform: the high-pass is covered elsewhere, and what is under test here
    is what the PASS does with the flags — which is where the bump confound
    lives.
    """
    n = int(round(seconds / self.DT))
    marks = {int((i + 1) * n / reversals) - 1 for i in range(reversals)} if reversals else set()
    for i in range(n):
      effort.update(self.DT, v, a_lat / (v * v), 0.0, 0.0, False,
                    pitch_rate_deg_s=pitch)
      effort.reversal = i in marks
      effort.limited = limited
      effort.a_lat = a_lat
      pass_.add(effort, self.DT, v)

  def test_a_quiet_pass_is_clean_and_fully_measurable(self):
    """The control case: no bumps, no sawing. Severity 0, nothing excised."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 4.0)
    assert p.clean_fraction() == pytest.approx(1.0)
    assert p.usable()
    assert p.verdict()[1] == 0.0

  def test_sawing_with_no_bump_still_condemns_the_pass(self):
    """THE GUARD MUST NOT DISARM THE FEATURE. 16 reversals over a clean 5 s
    pass is 3.2/s, exactly 2x OSC_RATE_LIMIT, so severity is exactly 2.0."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 5.0, reversals=16)
    assert p.clean_fraction() == pytest.approx(1.0)
    a_peak, sev = p.verdict()
    assert sev == pytest.approx(2.0, abs=0.01)
    assert a_peak == pytest.approx(2.0)

  def test_the_same_sawing_caused_by_a_bump_does_not(self):
    """THE REPORTED FAILURE, FIXED. Identical steering behaviour, but the
    pitch rate says the road was hitting the car. The disturbed samples leave
    the statistic entirely, so the measured rate is that of the quiet part --
    which here is zero."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 1.0)                             # quiet run-in
    self._frames(p, e, 1.0, pitch=7.0, reversals=4)     # the crossing
    self._frames(p, e, 3.0)                             # quiet run-out
    assert p.verdict()[1] == 0.0
    assert p.usable()

  def test_the_bump_window_outlives_the_bump(self):
    """v3.3.8 measured the oscillation STARTING AFTER the pitch rate decayed,
    because the lateral delay buffer replays the corrupted measurement as a
    corrupted setpoint ~0.5 s later. A gate that closed with the bump would
    miss exactly the frames it exists for.

    MUTATION: set DISTURB_HOLD_S to 0."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 0.1, pitch=7.0)                  # the bump itself
    self._frames(p, e, 0.5, reversals=2)                # the late ringing
    assert p.reversals == 0
    assert p.clean_duration == pytest.approx(0.0, abs=1e-9)

  def test_the_hold_does_expire(self):
    """...and it must, or one bump early in a drive would suppress every
    measurement after it. DISTURB_HOLD_S of quiet re-arms the counter."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 0.1, pitch=7.0)
    self._frames(p, e, CE.DISTURB_HOLD_S)               # ride the hold out
    before = p.clean_duration
    self._frames(p, e, 2.0, reversals=6)
    assert p.clean_duration > before + 1.9
    assert p.reversals == 6

  def test_disturbed_time_leaves_numerator_and_denominator_together(self):
    """THE TRAP THIS DESIGN AVOIDS. Dropping the reversals but keeping the
    time would make a bumpy pass look CLEANER than it was, which raises the
    floor and buys speed off a measurement that was never taken -- the
    dangerous direction.

    The measurable part of this pass saws at exactly OSC_RATE_LIMIT, so the
    verdict must be exactly 1.0 -- the threshold at which the ceiling comes
    down. Dividing by the FULL duration instead gives 0.61, which lands in
    the between-CLEAN_TH-and-1.0 dead band where a pass moves nothing at all.
    So the mutation does not merely shade the number, it silently converts a
    corner that told us it was too fast into one that told us nothing.

    MUTATION: divide by `duration` instead of `clean_duration`."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 2.0, pitch=7.0, reversals=16)    # disturbed
    self._frames(p, e, CE.DISTURB_HOLD_S)               # let the hold lapse
    self._frames(p, e, 5.0, reversals=8)                # clean, exactly at limit
    sev = p.verdict()[1]
    naive = (p.reversals / p.duration) / CE.OSC_RATE_LIMIT
    assert sev == pytest.approx(1.0, abs=0.02)
    assert naive == pytest.approx(0.61, abs=0.02)
    # the ceiling-moving threshold is 1.0; the mutant lands two thirds of the
    # way down, inside the dead band, where the pass teaches nothing
    assert sev > 0.98
    assert naive < 0.7

  def test_a_mostly_disturbed_pass_is_discarded_not_trusted(self):
    """When there is almost nothing left to measure, the honest answer is
    'no measurement', not a rate computed over a sliver.

    THE CLEAN PART IS DELIBERATELY LONGER THAN MIN_PASS_S HERE. An earlier
    version of this test used a 0.5 s clean window and PASSED ITS MUTATION,
    because `usable()` was rejecting the pass on the duration floor and never
    reaching the fraction check at all — the vacuous shape v3.4.5 named. A
    long bend that is mostly level crossing is the case only this guard
    catches.

    MUTATION: drop the MIN_CLEAN_FRAC check."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 2.0)                             # comfortably over MIN_PASS_S
    self._frames(p, e, 8.0, pitch=7.0)
    assert p.clean_duration > CE.MIN_PASS_S             # the duration floor is satisfied...
    assert p.clean_fraction() < CE.MIN_CLEAN_FRAC       # ...so only the fraction can reject it
    assert not p.usable()

  def test_the_limit_flag_is_excised_too(self):
    """A bump can saturate the controller just as easily as it can provoke a
    correction, so the OTHER severity component needs the same treatment.

    MUTATION: guard the reversals but keep accumulating limit_time."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 2.0, pitch=7.0, limited=True)
    self._frames(p, e, CE.DISTURB_HOLD_S)
    self._frames(p, e, 2.0)
    assert p.limit_time == pytest.approx(0.0, abs=1e-9)
    assert p.verdict()[1] == 0.0

  def test_how_fast_we_went_is_recorded_regardless(self):
    """`a_peak` and `v_min` are facts about the car, and the road surface does
    not change them. Only the EFFORT statistics are contaminated by a bump."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 3.0, pitch=7.0, a_lat=2.7, v=18.0)
    assert p.a_peak == pytest.approx(2.7)
    assert p.v_min == pytest.approx(18.0)

  def test_no_pitch_signal_is_the_old_behaviour_exactly(self):
    """read_pitch_rate returns 0.0 on any doubt, so an unreadable heartbeat
    must leave every pass measured as it was before this guard existed.

    MUTATION: default `pitch_rate_deg_s` to something non-zero, or fail the
    reader toward 'disturbed'."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    self._frames(p, e, 5.0, pitch=0.0, reversals=16)
    assert p.clean_duration == pytest.approx(p.duration)
    assert p.verdict()[1] == pytest.approx(2.0, abs=0.01)

  def test_garbage_pitch_does_not_suppress_a_measurement(self):
    """NaN/inf must read as 'not disturbed'. Failing the other way would
    silently stop the store learning anything at all."""
    for bad in (float('nan'), float('inf'), None, "x"):
      p, e = CE.CornerPass(), CE.LateralEffort()
      p.begin()
      self._frames(p, e, 5.0, pitch=bad, reversals=16)
      assert p.clean_duration == pytest.approx(p.duration)
      assert p.verdict()[1] == pytest.approx(2.0, abs=0.01)

  def test_the_threshold_matches_the_bump_damper(self):
    """Both answer 'is the road hitting the car right now'. Two thresholds
    for one question drift; this fails the day someone tunes one of them."""
    from openpilot.selfdrive.controls.lib.bump_damper import TRIGGER_DEG_S
    assert CE.DISTURB_DEG_S == TRIGGER_DEG_S
