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
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_speed as CE_CS


DT = 0.01     # the carState rate this is actually fed at


def drive(effort, seconds, angle_fn, v=20.0, curv=0.01, torque=0.0,
          lat_active=True, saturated=False, eps_limited=False, pass_=None):
  """Feed `effort` for `seconds`, steering by `angle_fn(t)`.

  `lat_active` DEFAULTS TRUE since v3.6.4: a pass openpilot did not steer is
  not a measurement of the corner (see MIN_ENGAGED_FRAC), so the engaged case
  is now the normal one to build a fixture from.
  """
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
      effort.update(self.DT, v, a_lat / (v * v), 0.0, 0.0, True,
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


class TestLaneDepartureGeometry:
  """FunnyPilot v3.6.4 — the fourth signal, and the most direct one.

  The other three ask how hard the CONTROLLER worked. This one asks whether the
  car stayed where it belonged, which is what "too fast for the bend" means
  physically.

  EVERY VALUE HERE IS IN openpilot's DEVICE FRAME: x forward, **y positive
  RIGHT** (common/transformations/camera.py:73), so `y_left` is NEGATIVE and
  `y_right` positive. v3.6.4 wrote this class with the signs the other way
  round; the result was that `width` came out negative, the plausibility gate
  rejected every frame, and the whole signal read a constant zero for a
  release. See test_the_signal_is_not_silently_dead.
  """
  L, R = -1.85, 1.85          # a centred car in a 3.7 m lane
  P = 0.9                     # confident lane lines

  def test_centred_is_zero(self):
    assert CE.lane_departure_m(self.L, self.R, self.P, self.P) == 0.0

  def test_the_signal_is_not_silently_dead(self):
    """THE v3.6.5 REGRESSION GUARD, and the one that pins the frame convention.

    MUTATION: swap the signs back (width = y_left - y_right, and the max()
    terms with them). Every case below then falls through the width gate to
    0.0 — which is exactly how the bug hid, because 0.0 is also the honest
    answer for 'inside the lane' and for 'cannot tell'. So a test that only
    checks the zero cases cannot see it; the departure has to be MEASURED.
    """
    real = CE.lane_departure_m(-0.60, 3.10, self.P, self.P)
    assert real > 0.0
    # 0.93 half-track + 0.04 camera offset - 0.60 to the line, less the 0.10
    # deadband v3.6.5 added for the model's x=0 extrapolation noise
    assert real == pytest.approx(0.27, abs=0.01)

  def test_inside_the_lines_is_still_zero(self):
    """Drifting within the lane is not a departure. MUTATION: drop the max(0,
    ...) and every ordinary corner would report a 'departure'."""
    assert CE.lane_departure_m(-2.3, 1.4, self.P, self.P) == 0.0

  def test_crossing_the_left_line_measures_the_overhang(self):
    """Left line 0.60 m from the device centreline, our left edge 0.93 + 0.04 m
    out, less the v3.6.5 deadband."""
    d = CE.lane_departure_m(-0.60, 3.10, self.P, self.P)
    assert d == pytest.approx(CE.HALF_TRACK_M + CE.CAMERA_OFFSET_M - 0.60
                              - CE.DEPART_DEADBAND_M)

  def test_the_deadband_swallows_the_extrapolation_noise(self):
    """v3.6.5 — THE REPORTED FALSE POSITIVE. `laneLines[i].y[0]` is at x = 0,
    beside the car, which the forward camera cannot see: the model infers it,
    and the inference wanders by a few centimetres. Dead centre in a lane was
    reading 5-6 cm of "departure". MUTATION: drop DEPART_DEADBAND_M."""
    # 6 cm of apparent overhang — the magnitude that was reported
    y_l = -CE.HALF_TRACK_M - CE.CAMERA_OFFSET_M + 0.06
    assert CE.lane_departure_m(y_l, y_l + 3.6, self.P, self.P) == 0.0

  def test_the_deadband_is_a_shift_not_a_step(self):
    """Subtracted rather than thresholded, so the signal stays continuous: a
    step at the deadband would make DEPART_LIMIT_M mean two different things
    either side of it. MUTATION: `return 0 if out < DEADBAND else out`."""
    y_l = -CE.HALF_TRACK_M - CE.CAMERA_OFFSET_M + CE.DEPART_DEADBAND_M + 0.01
    d = CE.lane_departure_m(y_l, y_l + 3.6, self.P, self.P)
    assert 0.0 < d < 0.02

  def test_crossing_the_right_line_is_symmetric(self):
    """Which way we fell out of the lane says nothing about the corner.

    NOTE this assertion was VACUOUS in v3.6.4 — with the signs inverted both
    sides returned 0.0 and were trivially equal. Both sides are non-zero now,
    which is asserted so it cannot go quiet again."""
    left = CE.lane_departure_m(-0.60 - CE.CAMERA_OFFSET_M, 3.10 - CE.CAMERA_OFFSET_M,
                               self.P, self.P)
    right = CE.lane_departure_m(-3.10 - CE.CAMERA_OFFSET_M, 0.60 - CE.CAMERA_OFFSET_M,
                                self.P, self.P)
    assert left > 0.0 and right > 0.0
    assert left == pytest.approx(right)

  def test_an_unconfident_line_measures_nothing(self):
    """A guessed line is a reason to measure NOTHING, not to measure something
    wrong. MUTATION: drop the probability gate."""
    assert CE.lane_departure_m(-0.60, 3.10, 0.1, self.P) == 0.0
    assert CE.lane_departure_m(-0.60, 3.10, self.P, 0.1) == 0.0

  def test_an_implausible_lane_width_measures_nothing(self):
    """Outside LANE_W_MIN/MAX the model has latched a road edge or the far
    side of a junction, and a departure computed from that is fiction."""
    assert CE.lane_departure_m(-0.90, 0.60, self.P, self.P) == 0.0     # 1.5 m
    assert CE.lane_departure_m(-4.0, 4.0, self.P, self.P) == 0.0       # 8 m

  def test_it_is_bounded(self):
    """One swerve is not a measurement of the corner."""
    assert CE.lane_departure_m(1.0, 4.7, self.P, self.P) == CE.MAX_DEPART_M

  def test_garbage_measures_nothing(self):
    for a, b in ((float('nan'), 1.85), (-1.85, float('inf')), (None, 1.85)):
      assert CE.lane_departure_m(a, b, self.P, self.P) == 0.0


class TestLaneDepartureDrivesSeverity:
  """It has to actually reach the learned value, or it is only a readout."""
  DT = 0.01

  def _pass_with(self, departure, seconds=4.0, lane_change=False):
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    for _ in range(int(seconds / self.DT)):
      e.update(self.DT, 20.0, 0.005, 0.0, 0.0, True,
               departure_m=departure, lane_change=lane_change)
      e.a_lat = 2.0
      p.add(e, self.DT, 20.0)
    return p

  def test_staying_in_lane_is_a_clean_pass(self):
    p = self._pass_with(0.0)
    assert p.depart_peak == 0.0
    assert p.verdict()[1] == 0.0

  def test_a_quarter_metre_out_is_exactly_at_the_limit(self):
    """DEPART_LIMIT_M is the point at which the departure ALONE lowers the
    corner's ceiling. MUTATION: drop the departure term from verdict()."""
    p = self._pass_with(CE.DEPART_LIMIT_M)
    assert p.verdict()[1] == pytest.approx(1.0)

  def test_further_out_is_proportionally_worse(self):
    """Continuous, like the other two: 'how far past' rather than a flag."""
    p = self._pass_with(0.5)
    assert p.verdict()[1] == pytest.approx(2.0)

  def test_it_is_a_peak_not_a_rate(self):
    """A departure is a fact about the corner and does not have to last for the
    bend to count. Half a second of it in a four second pass must register.

    v3.6.5 — the peak is now taken on a DEPART_TAU_S low pass, so half a second
    reaches most of the way rather than all of it. The looseness in the bound
    is that filter, not slack in the requirement."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    for i in range(400):
      d = 0.4 if 100 <= i < 150 else 0.0
      e.update(self.DT, 20.0, 0.005, 0.0, 0.0, True, departure_m=d)
      e.a_lat = 2.0
      p.add(e, self.DT, 20.0)
    assert 0.3 < p.depart_peak <= 0.4
    assert p.verdict()[1] > 1.0

  def test_a_single_frame_of_noise_is_not_a_departure(self):
    """v3.6.5 — THE OTHER HALF OF THE REPORTED PROBLEM. A PEAK over a four
    second pass of a signal derived from an extrapolation is the noise floor,
    not the signal: one frame set `depart_peak` for the whole bend. v3.6.4
    argued "it does not have to persist to count", which is right about the
    ROAD and wrong about the SENSOR. MUTATION: remove the DEPART_TAU_S filter
    and one frame condemns the corner again."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    for i in range(400):
      d = 0.6 if i == 100 else 0.0
      e.update(self.DT, 20.0, 0.005, 0.0, 0.0, True, departure_m=d)
      e.a_lat = 2.0
      p.add(e, self.DT, 20.0)
    assert p.depart_peak < 0.05
    assert p.verdict()[1] < CE.CLEAN_TH

  def test_a_lane_change_is_not_a_lane_departure(self):
    """Crossing a line on purpose says nothing about the corner. MUTATION:
    drop the lane_change suppression and every lane change inside a bend
    teaches that the bend is slow."""
    p = self._pass_with(0.6, lane_change=True)
    assert p.depart_peak == 0.0
    assert p.verdict()[1] == 0.0

  def test_a_bump_induced_departure_is_excised_like_the_others(self):
    """The road can throw the car out of the lane as easily as it can provoke
    a correction, so the disturbance gate covers this signal too."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    for _ in range(400):
      e.update(self.DT, 20.0, 0.005, 0.0, 0.0, True,
               pitch_rate_deg_s=7.0, departure_m=0.6)
      e.a_lat = 2.0
      p.add(e, self.DT, 20.0)
    assert p.depart_peak == 0.0


class TestOnlyOpenpilotsOwnPassesCount:
  """FunnyPilot v3.6.4 — a pass the DRIVER steered is not evidence about the
  corner. Drift wide because you are tired and every signal still live with
  lateral off reports it as "this corner is too fast"."""
  DT = 0.01

  def _pass(self, engaged_frac=1.0, seconds=4.0):
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    n = int(seconds / self.DT)
    for i in range(n):
      e.update(self.DT, 20.0, 0.005, 0.0, 0.0, i < n * engaged_frac)
      e.a_lat = 2.0
      p.add(e, self.DT, 20.0)
    return p

  def test_an_engaged_pass_is_usable(self):
    p = self._pass(1.0)
    assert p.engaged_fraction() == pytest.approx(1.0)
    assert p.usable()

  def test_a_hand_driven_pass_is_not(self):
    """MUTATION: drop the MIN_ENGAGED_FRAC term from usable(). This is the
    whole change — a bend the driver steered must teach the store nothing."""
    p = self._pass(0.0)
    assert p.engaged_fraction() == 0.0
    assert not p.usable()

  def test_a_single_dropped_frame_does_not_discard_it(self):
    """MIN_ENGAGED_FRAC is a fraction rather than an all-or-nothing flag so
    one late frame at a boundary cannot throw away a real measurement."""
    p = self._pass(0.99)
    assert p.engaged_fraction() > CE.MIN_ENGAGED_FRAC
    assert p.usable()


class TestTheDriversVerdict:
  """FunnyPilot v3.6.5 — a takeover is the fifth signal.

  THIS DOES NOT REOPEN WHAT v3.6.4 CLOSED, and the class above pins the half
  that still stands. v3.6.4 throws out passes the driver DROVE, because the
  severity signals then measure the human. v3.6.5 keeps passes openpilot drove
  and the human INTERRUPTED — which is not a measurement of the human at all,
  it is their judgement of OUR speed.

  The predecessor of the first test here asserted that a mid-corner takeover
  DISCARDED the pass. It pinned exactly the behaviour this release reverses,
  so keeping it would have been keeping the defect.
  """
  DT = 0.01

  def _pass(self, seconds=4.0, take_at=None, start_engaged=True, lead=False,
            brake=False, reversals=False, release_at=None):
    """Drive a bend, optionally handing an axis back at `take_at` seconds."""
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    n = int(seconds / self.DT)
    for i in range(n):
      t = i * self.DT
      taken = take_at is not None and t >= take_at
      if release_at is not None and t >= release_at:
        taken = False
      lat = start_engaged and not (taken and not brake)
      e.update(self.DT, 20.0, 0.005,
               (10.0 if (i // 5) % 2 else -10.0) if reversals else 0.0,
               0.0, lat, brake_pressed=bool(taken and brake))
      e.a_lat = 2.0
      p.add(e, self.DT, 20.0, lead=lead)
    return p

  def test_a_takeover_is_the_drivers_verdict_not_a_discard(self):
    """MUTATION: drop the takeover term from verdict(). The pass then reports
    a clean 0.0 severity and the corner it was too fast for gets RAISED."""
    p = self._pass(take_at=2.0)
    assert p.took_over
    assert p.usable()
    assert p.verdict()[1] == pytest.approx(CE.TAKEOVER_SEVERITY)

  def test_the_brake_pedal_no_longer_condemns_the_corner(self):
    """v3.6.6 REVERSES THE v3.6.5 HALF OF THIS ON PURPOSE, and the owner's rule
    is why: "if we take manual control of long going through a corner, the speed
    we take it at should only ever RAISE the corner's speed". Braking through a
    45 mph bend is a statement about the driver's appetite, not about the bend —
    they may be behind traffic or simply unsure of the road — so it must not
    leave that bend permanently slower.

    The pass is not discarded, it is RAISE-ONLY (see scc_learn_store.observe),
    and the lateral signals measured during it still lower the ceiling on their
    own evidence. Only the SPEED is removed from the argument.

    MUTATION: put brake back into `override`."""
    p = self._pass(take_at=2.0, brake=True)
    assert not p.took_over
    assert p.long_manual
    assert p.verdict()[1] == pytest.approx(0.0)

  def test_the_accelerator_is_deliberately_absent(self):
    """A driver adding throttle mid-bend is evidence we were too SLOW. Folding
    it in here would let the one signal arguing for more speed lower the
    ceiling. Pinned on the signature, the way the v3.4.0 status dot pinned the
    absence of a pitch term."""
    e = CE.LateralEffort()
    # v3.6.6 — NO longitudinal state is a takeover any more, pedal up or down,
    # because a manual speed choice can only ever RAISE a corner. What it does
    # set is `long_manual`, which makes the pass raise-only.
    for gas, brake, long_on in ((True, False, False), (False, True, True),
                                (False, False, False)):
      e = CE.LateralEffort()
      e.update(0.01, 20.0, 0.005, 0.0, 0.0, True, long_active=long_on,
               gas_pressed=gas, brake_pressed=brake)
      assert not e.override, (gas, brake, long_on)
      assert e.long_manual, (gas, brake, long_on)
    # the WHEEL is still a takeover
    e2 = CE.LateralEffort()
    e2.update(0.01, 20.0, 0.005, 0.0, 0.0, False)
    assert e2.override

  def test_a_pass_that_was_never_ours_is_not_a_takeover(self):
    """v3.6.4's rule, unchanged: the human driving the whole bend teaches
    nothing. MUTATION: drop `engaged_at_start` and a hand-driven pass becomes
    a maximum-severity condemnation of the corner on its first frame."""
    p = self._pass(seconds=4.0, start_engaged=False)
    assert not p.took_over
    assert not p.usable()

  def test_a_lead_disarms_the_verdict_but_still_ends_the_pass(self):
    """The ONE exclusion this design needs. Braking for a car that slowed in
    front of us says nothing about the bend, and on a first visit `seed=True`
    would adopt that outright. MUTATION: drop the `lead` term."""
    p = self._pass(take_at=2.0, lead=True)
    assert not p.took_over
    assert p.verdict()[1] == pytest.approx(0.0)
    # ...but nothing after the handover was measured
    assert p.duration == pytest.approx(2.0, abs=0.05)

  def test_one_dropped_frame_is_not_a_takeover(self):
    """`latActive` drops for a frame at plenty of boundaries that are not
    interventions. MUTATION: remove TAKEOVER_DWELL_S and every one of them
    condemns the corner it happened in."""
    p = self._pass(take_at=2.0, release_at=2.0 + self.DT * 3)
    assert not p.took_over
    assert p.usable()
    assert p.verdict()[1] == pytest.approx(0.0)

  def test_the_dwell_is_shorter_than_a_real_grab(self):
    """Held for longer than the dwell, it latches. The pair of tests is the
    point: one frame must not, a real intervention must."""
    p = self._pass(take_at=2.0, release_at=2.0 + CE.TAKEOVER_DWELL_S + 0.1)
    assert p.took_over

  def test_an_early_takeover_still_counts(self):
    """A takeover is an EVENT, not a rate, so MIN_PASS_S does not apply to it —
    and requiring it would discard exactly the case that matters most, a driver
    grabbing the wheel because the car entered far too fast. MUTATION: keep the
    MIN_PASS_S branch for takeovers."""
    p = self._pass(seconds=2.0, take_at=0.5)
    assert p.duration < CE.MIN_PASS_S
    assert p.usable()
    assert p.verdict()[1] == pytest.approx(CE.TAKEOVER_SEVERITY)

  def test_an_instant_takeover_is_still_too_little(self):
    """MIN_TAKEOVER_S: the car has to have actually been in the bend."""
    p = self._pass(seconds=2.0, take_at=0.1)
    assert p.took_over
    assert not p.usable()

  def test_a_short_pass_does_not_invent_a_rate(self):
    """THE TRAP IN EXEMPTING THE WINDOW. One reversal in 0.4 s is a rate of
    2.5/s — over the limit — off a sample far too short to mean it. The rate
    terms are DROPPED below MIN_PASS_S of clean window rather than computed
    anyway, so a takeover reports the takeover and nothing invented.
    MUTATION: compute the rates unconditionally; severity leaves 2.0."""
    p = self._pass(seconds=2.0, take_at=0.5, reversals=True)
    assert p.reversals > 0
    assert p.clean_duration < CE.MIN_PASS_S
    assert p.verdict()[1] == pytest.approx(CE.TAKEOVER_SEVERITY)

  def test_nothing_after_the_takeover_is_measured(self):
    """Everything past the intervention is the human driving, which is what
    v3.6.4 rules out. MUTATION: keep accumulating; `engaged_fraction` then
    collapses and the pass this signal exists to commit is rejected."""
    p = self._pass(seconds=6.0, take_at=2.0)
    assert p.duration == pytest.approx(2.0, abs=0.05)
    assert p.engaged_fraction() == pytest.approx(1.0)

  def test_a_takeover_beats_a_clean_reading(self):
    """It enters through the same max() as the other four, so a pass that
    looked clean right up to the intervention still reports the verdict."""
    clean = self._pass(seconds=4.0)
    assert clean.verdict()[1] == pytest.approx(0.0)
    assert self._pass(seconds=4.0, take_at=2.0).verdict()[1] > clean.verdict()[1]


class TestTheDriversDemonstration:
  """FunnyPilot v3.6.5 — the mirror of the takeover, and the only signal in this
  file that can make a corner FASTER on one pass.

  openpilot is steering, nothing is stressed, the driver holds the throttle.
  Whatever the car reaches under those conditions is not an estimate of what the
  corner supports — it is a demonstration that it supports it.
  """
  DT = 0.01

  def _pass(self, seconds=4.0, gas_from=None, lat=True, sawing=False,
            brake=False):
    p, e = CE.CornerPass(), CE.LateralEffort()
    p.begin()
    n = int(seconds / self.DT)
    for i in range(n):
      t = i * self.DT
      gas = gas_from is not None and t >= gas_from
      e.update(self.DT, 20.0, 0.005,
               30.0 + (5.0 if (i // 5) % 2 else -5.0) if sawing else 30.0,
               0.0, lat, long_active=not gas, gas_pressed=gas,
               brake_pressed=brake and gas)
      e.a_lat = 3.0
      p.add(e, self.DT, 20.0)
    return p

  def test_gas_with_lateral_engaged_is_a_demonstration(self):
    """MUTATION: drop the `demo` term from LateralEffort, or the demo_time
    accumulation from CornerPass.add."""
    p = self._pass(gas_from=1.0)
    assert p.demo_time >= CE.MIN_DEMO_S
    assert p.demonstrated()
    assert p.usable()

  def test_it_is_not_a_takeover(self):
    """THE BUG v3.6.5 SHIPPED. `controlsd` clears `longActive` for any
    `overrideLongitudinal` event and a gas press is one, so without the pedal
    itself a driver asking for more speed was indistinguishable from one
    switching longitudinal off — and scored 2.0. MUTATION: remove the
    `and not gas` term from `override`."""
    p = self._pass(gas_from=1.0)
    assert not p.took_over
    assert p.verdict()[1] < CE.CLEAN_TH

  def test_a_tap_is_not_a_demonstration(self):
    """MIN_DEMO_S. One dab of the pedal says nothing about the whole bend."""
    p = self._pass(seconds=4.0, gas_from=3.9)
    assert not p.demonstrated()

  def test_a_stressed_pass_is_never_a_demonstration(self):
    """THE GUARD THAT MAKES THIS SAFE. Throttle through a bend the car is
    sawing at is not proof the bend supports it — it is proof it does not.
    MUTATION: drop the CLEAN_TH term from demonstrated()."""
    p = self._pass(gas_from=1.0, sawing=True)
    assert p.verdict()[1] >= CE.CLEAN_TH
    assert not p.demonstrated()

  def test_the_driver_steering_is_never_a_demonstration(self):
    """v3.6.4's rule is untouched: with the human on the wheel the speed they
    choose says nothing about what the CONTROLLER can do through the bend."""
    p = self._pass(gas_from=1.0, lat=False)
    assert not p.demonstrated()

  def test_braking_marks_the_pass_raise_only_and_moves_nothing(self):
    """v3.6.6 — the brake no longer condemns the corner. It also cannot make it
    faster, and NOT because `demonstrated()` rejects it: seeding never bypasses
    update_interval's direction guards, and a pass the driver braked through has
    a LOW `a_peak`, which may not lower a floor. The two mechanisms compose to
    exactly the owner's rule — a 45 mph bend taken by hand at 35 stays 45."""
    p = self._pass(gas_from=1.0, brake=True)
    assert not p.took_over
    assert p.long_manual
    lo, hi = CE_CS.update_interval(2.4, 3.0, a_peak=p.verdict()[0] * 0.4,
                                  severity=0.0, seed=True)
    assert lo == pytest.approx(2.4), "a slow manual pass must not lower the floor"
    assert hi == pytest.approx(3.0), "...nor touch the ceiling"
