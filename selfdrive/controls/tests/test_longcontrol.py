import pytest

from cereal import car, custom
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.longcontrol import (LongControl, LongCtrlState, STARTING_ACCEL_RATE,
                                                          STARTING_CREEP_RATE, starting_accel_rate,
                                                          long_control_state_trans)


def _make_long_control(starting_state=False):
  CP = car.CarParams.new_message()
  CP.longitudinalTuning.kpBP = [0.]
  CP.longitudinalTuning.kpV = [1.0]
  CP.longitudinalTuning.kiBP = [0.]
  CP.longitudinalTuning.kiV = [0.1]
  CP.startingState = starting_state
  CP.vEgoStarting = 0.5
  CP.startAccel = 1.5
  CP.stopAccel = -2.0
  CP.stoppingDecelRate = 0.40  # the K5's value (Hyundai DEFAULT config)
  CP_SP = custom.CarParamsSP.new_message()
  return LongControl(CP, CP_SP)




class TestLongControlStateTransition:

  def test_stay_stopped(self):
    CP = car.CarParams.new_message()
    CP_SP = custom.CarParamsSP.new_message()
    active = True
    current_state = LongCtrlState.stopping
    next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=True, brake_pressed=False, cruise_standstill=False)
    assert next_state == LongCtrlState.stopping
    next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=False, brake_pressed=True, cruise_standstill=False)
    assert next_state == LongCtrlState.stopping
    next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=False, brake_pressed=False, cruise_standstill=True)
    assert next_state == LongCtrlState.stopping
    next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=1.0,
                             should_stop=False, brake_pressed=False, cruise_standstill=False)
    assert next_state == LongCtrlState.pid
    active = False
    next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=1.0,
                             should_stop=False, brake_pressed=False, cruise_standstill=False)
    assert next_state == LongCtrlState.off

def test_engage():
  CP = car.CarParams.new_message()
  CP_SP = custom.CarParamsSP.new_message()
  active = True
  current_state = LongCtrlState.off
  next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=True, brake_pressed=False, cruise_standstill=False)
  assert next_state == LongCtrlState.stopping
  next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=False, brake_pressed=True, cruise_standstill=False)
  assert next_state == LongCtrlState.stopping
  next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=False, brake_pressed=False, cruise_standstill=True)
  assert next_state == LongCtrlState.stopping
  next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=False, brake_pressed=False, cruise_standstill=False)
  assert next_state == LongCtrlState.pid

def test_starting():
  CP = car.CarParams.new_message(startingState=True, vEgoStarting=0.5)
  CP_SP = custom.CarParamsSP.new_message()
  active = True
  current_state = LongCtrlState.starting
  next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=0.1,
                             should_stop=False, brake_pressed=False, cruise_standstill=False)
  assert next_state == LongCtrlState.starting
  next_state = long_control_state_trans(CP, CP_SP, active, current_state, v_ego=1.0,
                             should_stop=False, brake_pressed=False, cruise_standstill=False)
  assert next_state == LongCtrlState.pid

def test_bumpless_pid_entry():
  # v3.2.6e: entering pid from stopping must not step the output — the
  # integrator is seeded so the first PID frame continues from the last
  # commanded accel.
  LC = _make_long_control()
  LC.long_control_state = LongCtrlState.stopping
  LC.last_output_accel = -0.4
  CS = car.CarState.new_message(vEgo=5.0, aEgo=-0.4)
  out = LC.update(True, CS, a_target=0.2, should_stop=False, accel_limits=[-3.5, 2.0])
  assert LC.long_control_state == LongCtrlState.pid
  assert abs(out - (-0.4)) < 0.05, f"pid entry stepped the output: {out}"

def test_starting_ramp():
  # v3.2.6e: the starting state slews toward startAccel instead of stepping.
  LC = _make_long_control(starting_state=True)
  LC.long_control_state = LongCtrlState.stopping
  LC.last_output_accel = -0.3
  CS = car.CarState.new_message(vEgo=0.1, aEgo=0.0)
  out = LC.update(True, CS, a_target=0.5, should_stop=False, accel_limits=[-3.5, 2.0])
  assert LC.long_control_state == LongCtrlState.starting
  # v3.5.4: the rate is scheduled on the current accel — still brisk here
  # because -0.3 is brake release, not launch.
  assert abs(out - (-0.3 + starting_accel_rate(-0.3) * DT_CTRL)) < 1e-6
  for _ in range(200):
    out = LC.update(True, CS, a_target=0.5, should_stop=False, accel_limits=[-3.5, 2.0])
  assert abs(out - LC.CP.startAccel) < 1e-6


def test_starting_rate_is_brisk_while_releasing_the_brake():
  """v3.5.4. The ramp does two jobs. While the output is NEGATIVE it is
  releasing the brake, and slowing that is a car that sits at a green light —
  so the full rate must survive there."""
  assert starting_accel_rate(-2.0) == STARTING_ACCEL_RATE
  assert starting_accel_rate(-1.0) == STARTING_ACCEL_RATE


def test_starting_rate_is_gentle_once_pulling_away():
  """...and once the output is POSITIVE it is applying launch torque, which is
  where the head-snap lives and the half worth softening."""
  assert starting_accel_rate(1.5) == STARTING_CREEP_RATE
  assert STARTING_CREEP_RATE < STARTING_ACCEL_RATE


def test_starting_rate_is_monotone_and_continuous():
  """MUTATION: flip the breakpoint order. Gentle brake release plus a snappy
  launch is exactly backwards, and reads as a plausible edit."""
  rates = [starting_accel_rate(a) for a in (-2.0, -0.5, 0.0, 0.5, 2.0)]
  assert rates == sorted(rates, reverse=True)
  assert rates[0] == STARTING_ACCEL_RATE and rates[-1] == STARTING_CREEP_RATE


def test_stopping_ramp_runs_at_the_cars_full_rate():
  """v3.5.5 REGRESSION GUARD. In the stopping state the PID is reset, so this
  ramp is the ONLY brake authority the car has — v3.5.4 scaled it to 0.35x
  while rolling and thereby slowed the whole completion of the stop, not just
  a final bite. MUTATION: reintroduce any speed-dependent scale here and the
  per-frame step stops matching CP.stoppingDecelRate exactly."""
  LC = _make_long_control()
  LC.long_control_state = LongCtrlState.pid
  LC.last_output_accel = -1.2
  CS = car.CarState.new_message(vEgo=2.0, aEgo=-1.2)
  CS.vEgo = 2.0  # still ROLLING: the case v3.5.4 tapered
  out = LC.update(True, CS, a_target=-1.2, should_stop=True, accel_limits=[-3.5, 2.0])
  assert LC.long_control_state == LongCtrlState.stopping
  # exactly one full-rate step of brake was added, with no scaling
  assert out == pytest.approx(-1.2 - LC.CP.stoppingDecelRate * DT_CTRL, abs=1e-9)


@pytest.mark.parametrize('last_accel', [1.0, 0.0, -1.0, -3.5])
def test_stopping_honors_stronger_planned_braking(last_accel):
  lc = _make_long_control()
  lc.long_control_state = LongCtrlState.pid
  lc.last_output_accel = last_accel
  cs = car.CarState.new_message(vEgo=2.0, aEgo=last_accel)
  out = lc.update(True, cs, a_target=-3.0, should_stop=True, accel_limits=[-3.5, 2.0])
  assert lc.long_control_state == LongCtrlState.stopping
  assert out <= min(last_accel, -3.0)


def test_stopping_keeps_hold_when_plan_releases_brake():
  lc = _make_long_control()
  lc.long_control_state = LongCtrlState.stopping
  lc.last_output_accel = -2.0
  cs = car.CarState.new_message(vEgo=0.0)
  assert lc.update(True, cs, 0.5, True, [-3.5, 2.0]) == -2.0
