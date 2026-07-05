from cereal import car, custom
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.longcontrol import (LongControl, LongCtrlState, STARTING_ACCEL_RATE,
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
  assert abs(out - (-0.3 + STARTING_ACCEL_RATE * DT_CTRL)) < 1e-6
  for _ in range(200):
    out = LC.update(True, CS, a_target=0.5, should_stop=False, accel_limits=[-3.5, 2.0])
  assert abs(out - LC.CP.startAccel) < 1e-6
