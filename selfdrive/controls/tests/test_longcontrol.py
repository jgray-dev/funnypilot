from cereal import car, custom
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.longcontrol import (LongControl, LongCtrlState, STARTING_ACCEL_RATE,
                                                          STARTING_CREEP_RATE, starting_accel_rate,
                                                          stopping_decel_rate, STOP_TAPER_V,
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
  assert starting_accel_rate(STOP_TAPER_V[0] * 0 - 1.0) == STARTING_ACCEL_RATE


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


def test_stopping_taper_is_gentle_rolling_and_full_at_rest():
  """v3.5.4. The last bite of brake pressure used to arrive at full rate while
  the car was still moving — the nod at the end of a stop. Below STOP_TAPER_V
  the full rate is restored so the hold is always secured firmly."""
  base = 0.8
  assert stopping_decel_rate(base, 0.0) == base
  assert stopping_decel_rate(base, STOP_TAPER_V[0]) == base
  assert stopping_decel_rate(base, 3.0) < base


def test_stopping_taper_never_inverts():
  """SAFETY: it may only ever SOFTEN the extra brake ramp, never amplify it.
  MUTATION: a scale above 1.0 would apply more brake than the car asked for."""
  base = 0.8
  for v in (0.0, 0.25, 0.5, 1.0, 1.5, 5.0, 40.0):
    assert 0.0 < stopping_decel_rate(base, v) <= base + 1e-9


def test_stopping_ramp_only_ever_adds_braking():
  """The ramp starts from last_output_accel — already whatever the planner
  asked for — and the taper only changes how fast MORE brake is added. It can
  never make the car brake less than commanded."""
  LC = _make_long_control()
  LC.long_control_state = LongCtrlState.pid
  LC.last_output_accel = -1.2
  CS = car.CarState.new_message(vEgo=2.0, aEgo=-1.2)
  out = LC.update(True, CS, a_target=-1.2, should_stop=True, accel_limits=[-3.5, 2.0])
  assert LC.long_control_state == LongCtrlState.stopping
  assert out <= -1.2 + 1e-9
