"""FunnyPilot v3.6.7 — the actuator layer's tracking of the planner's demand.

THIS IS THE LAST STAGE BEFORE CAN, and it is where three separate on-road
reports turned out to originate: late braking for a stopped car, sluggish
launch behind a lead, and a following loop that overshoots and corrects.

`_calculate_lookahead_jerk` sized the jerk allowance as
`(accel_cmd - accel_last) / future_t` — purely proportional to the remaining
gap. `jerk_limited_integrator` rate-limits by that, so the output is a
FIRST-ORDER LAG with time constant `future_t` (0.6 s at speed). A pure P
controller cannot track a ramp, and the planner's demand is a ramp almost all
the time, so the steady-state error is `future_t * d(accel_cmd)/dt` and it
persists for as long as the ramp does.

Import-light: stdlib plus the two modules under test.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../../.."))

from opendbc.car import DT_CTRL
from opendbc.car.hyundai.values import CAR
from opendbc.sunnypilot.car.hyundai.longitudinal import controller as C
from opendbc.sunnypilot.car.hyundai.values import HyundaiFlagsSP

STEP = DT_CTRL * 5      # the controller runs at 20 Hz (carcontroller: frame % 5)
LongCtrlState = C.LongCtrlState


class _Obj:
  def __init__(self, **kw):
    self.__dict__.update(kw)


def _controller(predictive=True, radar=True):
  """THE REAL LongitudinalController, with the smallest stubs that let it run.

  DRIVING THE REAL OBJECT IS THE WHOLE POINT. The first version of this file
  re-implemented the jerk law in a local rig and every mutation to
  controller.py survived it — a test that carries its own copy of the maths can
  only fail when the copy drifts, which is the v3.4.9 shape this repo has
  already been bitten by once.
  """
  flags = HyundaiFlagsSP.LONG_TUNING_PREDICTIVE if predictive else HyundaiFlagsSP.LONG_TUNING_DYNAMIC
  CP = _Obj(carFingerprint=CAR.KIA_K5_2021, flags=0, radarUnavailable=not radar)
  CP_SP = _Obj(flags=int(flags))
  return C.LongitudinalController(CP, CP_SP)


class _Rig:
  """One 20 Hz cycle of the real controller: jerk law, then integrator."""

  def __init__(self, v_ego=20.0, predictive=True, feed_forward=True):
    self.c = _controller(predictive=predictive)
    self.v_ego = v_ego
    self.ff_on = feed_forward

  def step(self, cmd, state=None):
    state = LongCtrlState.pid if state is None else state
    CC = _Obj(longActive=True,
              actuators=_Obj(accel=cmd, longControlState=state))
    CS = _Obj(out=_Obj(vEgo=self.v_ego, aEgo=self.c.accel_last), aBasis=self.c.accel_last)
    self.c.update(CC, CS)
    if not self.ff_on:
      # the pre-v3.6.7 law, reproduced by neutralising ONLY the feed-forward
      # state the real code reads — everything else stays the real code
      self.c.cmd_rate = 0.0
    return self.c.actual_accel

  @property
  def accel_last(self):
    return self.c.accel_last

  @property
  def cmd_rate(self):
    return self.c.cmd_rate

  @cmd_rate.setter
  def cmd_rate(self, v):
    self.c.cmd_rate = v

  def raw_cap(self, *a):
    raise NotImplementedError


def _ramp(rig, rate, seconds=1.2):
  """Follow a constant-rate command ramp; return the final tracking error.

  SHORT ON PURPOSE. `calculate_accel` clips the demand to the car's own
  [ACCEL_MIN, ACCEL_MAX] = [-3.5, 2.0], so a longer ramp measures that clip
  rather than the tracking law. Every rate below stays inside it."""
  cmd = 0.0
  for _ in range(int(seconds / STEP)):
    cmd += rate * STEP
    rig.step(cmd)
  return cmd - rig.accel_last


class TestTheLagThatWasThere:
  """What the old law did, measured — so the fix is not taken on faith."""

  def test_a_pure_p_law_falls_behind_a_brake_ramp(self):
    """MEASURED, through the real controller: 1.2 s of a 2 m/s^3 brake ramp
    left the car 0.54 m/s^2 short of the commanded deceleration, and it keeps
    growing toward `future_t * rate` (about 1.1) for as long as the ramp lasts.
    That is "it refuses to brake until it's too late"."""
    err = _ramp(_Rig(feed_forward=False), -2.0)
    assert err < -0.4

  def test_the_error_scales_with_the_ramp_rate(self):
    """`future_t * rate`, which is what makes it a tracking error rather than a
    delay you can wait out."""
    slow = abs(_ramp(_Rig(feed_forward=False), -1.0))
    fast = abs(_ramp(_Rig(feed_forward=False), -2.0))
    assert fast > slow * 1.5

  def test_below_MIN_JERK_the_floor_hid_it(self):
    """Why this survived so long: a demand moving slower than MIN_JERK is
    tracked perfectly, so gentle driving looked fine and only real braking
    exposed it."""
    assert abs(_ramp(_Rig(feed_forward=False), -0.4)) < 0.02


class TestTheFeedForwardMakesItPredictive:
  """The allowance now includes the rate the COMMAND is moving at, so the
  proportional term only closes the residual."""

  def test_a_brake_ramp_is_tracked_with_no_steady_state_error(self):
    """MUTATION: drop the `min(ff, 0.0)` term. The error returns to ~1 m/s^2.

    Rates up to the speed cap only. Past it the actuator is rate-limited by
    design and the residual is the CAP doing its job, not the tracking law
    failing — see test_beyond_the_cap_the_cap_is_what_binds."""
    for rate in (-0.5, -1.0, -2.0, -2.5):
      assert abs(_ramp(_Rig(), rate)) < 0.06, f"lagging on a {rate} m/s^3 ramp"

  def test_beyond_the_cap_the_cap_is_what_binds(self):
    """A demand steeper than the speed-based jerk limit cannot be followed, and
    should not be — that limit is the comfort bound and it is untouched. What
    matters is that the shortfall is now the CAP's, small and bounded, rather
    than `future_t * rate` and unbounded."""
    # the lower jerk cap is interp(v, [0,5,20], [5.0,3.5,3.0]), so a slow car is
    # allowed MORE jerk than a fast one
    tight = abs(_ramp(_Rig(v_ego=20.0), -4.0))
    loose = abs(_ramp(_Rig(v_ego=2.0), -4.0))
    assert loose < tight, "the speed cap is what limits a steeper-than-cap ramp"

  def test_a_throttle_ramp_too(self):
    """The same law runs on the accel side, which is the sluggish-launch half
    of the report. MUTATION: drop the `max(ff, 0.0)` term."""
    for rate in (0.5, 1.0, 1.5):
      assert abs(_ramp(_Rig(), rate)) < 0.06

  def test_the_feed_forward_only_helps_in_the_direction_of_travel(self):
    """A falling command rate must not shrink the UPPER allowance, nor a rising
    one the lower — the two are separate limits published to the car.

    DRIVEN AT THE FUNCTION, and the reason is worth recording: through the full
    loop this clamp CANNOT CHANGE ANY OUTPUT at the current FF_ALPHA. By the
    time the jerk is computed, `_update_command_rate` has already folded the new
    command in at alpha 0.4, so a reversal big enough to matter has already
    flipped the smoothed rate's sign — see
    test_the_direction_clamp_is_currently_subsumed_by_the_smoothing. It is kept
    because it is the statement of intent, and because lowering FF_ALPHA makes
    it live. MUTATION: use `ff` unclamped on either side."""
    c = _controller()
    c.cmd_rate = -3.0                       # a strongly falling command
    up_stale, _ = c._calculate_lookahead_jerk(1.0, 20.0)
    c.cmd_rate = 0.0
    up_clean, _ = c._calculate_lookahead_jerk(1.0, 20.0)
    assert up_stale == pytest.approx(up_clean), "a falling rate ate the upper allowance"

    c.cmd_rate = 3.0                        # ...and the mirror
    _, dn_stale = c._calculate_lookahead_jerk(-1.0, 20.0)
    c.cmd_rate = 0.0
    _, dn_clean = c._calculate_lookahead_jerk(-1.0, 20.0)
    assert dn_stale == pytest.approx(dn_clean), "a rising rate ate the lower allowance"

  def test_the_feed_forward_does_reach_the_jerk_in_the_matching_direction(self):
    """Anti-vacuous: the clamp must not be doing its job by discarding the
    feed-forward altogether."""
    c = _controller()
    c.cmd_rate = 0.0
    base, _ = c._calculate_lookahead_jerk(0.3, 20.0)
    c.cmd_rate = 1.5
    helped, _ = c._calculate_lookahead_jerk(0.3, 20.0)
    assert helped > base + 1.0

  def test_the_direction_clamp_is_currently_subsumed_by_the_smoothing(self):
    """The relationship, not the values — the same treatment scc_fusion gives
    its vision veto. For the clamp to change an output the SMOOTHED rate would
    have to keep the old sign through a reversal, and one step of
    `_update_command_rate` moves it by FF_ALPHA of the new raw rate. Lower
    FF_ALPHA far enough and the clamp starts doing real work, at which point it
    needs behavioural tests — which is exactly what this failing should
    prompt."""
    reachable_rate = 4.0            # car_config.jerk_limits bounds the command
    raw_after_reversal = 1.0 / STEP  # a 1 m/s^2 reversal in one 20 Hz step
    carried = (1.0 - C.FF_ALPHA) * -reachable_rate + C.FF_ALPHA * raw_after_reversal
    assert carried > 0.0, \
      "a stale opposite-sign command rate is now reachable - test the clamp"

  def test_it_can_never_exceed_the_command(self):
    """THE INVARIANT. This only ever raises a LIMIT; the output is still
    `rate_limit(..., desired_accel)`, so the actuator can track the planner
    faithfully and can never overshoot what the planner asked for."""
    rig = _Rig()
    for cmd in (-3.5, -1.0, 0.0, 1.5, -2.0, 0.5):
      for _ in range(120):
        out = rig.step(cmd)
        assert out <= max(cmd, 0.0) + 1e-9 or out >= min(cmd, 0.0) - 1e-9
      assert abs(rig.accel_last - cmd) < 1e-6

  def test_the_speed_caps_still_bound_it(self):
    """A big command step must not turn into an unbounded jerk. MUTATION:
    remove the `min(..., cap)`."""
    rig = _Rig()
    rig.step(-5.0)
    assert abs(rig.accel_last) <= 3.0 * STEP + 1e-9

  def test_a_step_still_takes_the_shape_of_a_ramp(self):
    """Snappier, not instant: the smoothing that makes this comfortable is the
    jerk limit, and it is untouched."""
    rig = _Rig()
    outs = [rig.step(-2.0) for _ in range(6)]
    assert outs == sorted(outs, reverse=True)
    assert outs[0] > -0.30, "the first step must not be the whole command"

  def test_the_smoothing_constant_is_sane(self):
    """FF_ALPHA is on a difference of a 20 Hz signal that is published to the
    car; unfiltered it would chatter the SCC12/SCC14 jerk field."""
    assert 0.0 < C.FF_ALPHA < 1.0


class TestTheLaunchFromAStop:
  """FunnyPilot v3.6.8 — the `starting` state's upper jerk allowance.

  `stopping` forces the command to 0.0, so the car comes to rest with the
  command there. `starting` then ramps it up under `_calculate_speed_based_
  jerk_limits`, and exits only at `v_ego > CP.vEgoStarting` = 0.1 m/s — so the
  whole ramp is paid BEFORE the car moves. At the old flat 0.5 m/s^3 that is a
  full second of dead time on every launch, which is the reported "hesitant to
  go after stopping behind a lead car at a traffic light".
  """

  def _upper(self, state, v=0.0):
    return C.LongitudinalController._calculate_speed_based_jerk_limits(v, state)[0]

  def test_the_starting_state_is_no_longer_the_flat_non_pid_default(self):
    assert self._upper(LongCtrlState.starting) == C.STARTING_UPPER_JERK
    assert C.STARTING_UPPER_JERK > 0.5

  def test_nothing_steps_at_the_boundary_the_car_crosses_a_moment_later(self):
    # `starting` ends at 0.1 m/s and `pid` takes over. If the two disagreed,
    # the allowance would jump at exactly the moment the car begins to move.
    assert self._upper(LongCtrlState.starting, 0.1) == pytest.approx(
      self._upper(LongCtrlState.pid, 0.1), abs=0.05)

  def test_stopping_keeps_the_slow_allowance(self):
    # Here the cap governs brake release while coming to rest, which is a place
    # where slow IS the point. Raising it would have been a silent second
    # change riding along with the launch fix.
    assert self._upper(LongCtrlState.stopping) == 0.5
    assert self._upper(LongCtrlState.off) == 0.5

  def test_the_dead_time_before_the_car_moves(self):
    # MEASURED, both ways, so the claim in the comment is checked rather than
    # asserted: how long to ramp the command to the +0.5 m/s^2 that actually
    # moves the car.
    def ramp_time(cap, target=0.5):
      a, t = 0.0, 0.0
      while a < target:
        a += cap * STEP
        t += STEP
      return t
    assert ramp_time(0.5) == pytest.approx(1.00, abs=0.03)
    assert ramp_time(self._upper(LongCtrlState.starting)) < 0.30

  def test_the_brake_allowance_is_untouched_in_every_state(self):
    # This release moves the UPPER limit only. A launch fix that also loosened
    # braking would be two changes wearing one name.
    for st in (LongCtrlState.off, LongCtrlState.pid, LongCtrlState.starting,
               LongCtrlState.stopping):
      for v in (0.0, 5.0, 20.0, 30.0):
        lower = C.LongitudinalController._calculate_speed_based_jerk_limits(v, st)[1]
        assert lower == pytest.approx(float(np.interp(v, [0.0, 5.0, 20.0], [5.0, 3.5, 3.0])))

  def test_a_launch_actually_gets_moving_through_the_real_controller(self):
    # End to end: the planner asks for +1.0 and the controller must deliver
    # something that moves the car inside half a second.
    rig = _Rig(v_ego=0.0)
    rig.c.long_control_state_last = LongCtrlState.starting
    out = [rig.step(1.0, state=LongCtrlState.starting) for _ in range(10)]
    assert out[-1] > 0.5, "half a second in and the command still cannot move the car"
