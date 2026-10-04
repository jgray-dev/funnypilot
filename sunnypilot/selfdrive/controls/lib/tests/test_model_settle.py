from types import SimpleNamespace as NS
import pytest
from openpilot.sunnypilot.selfdrive.controls.lib.model_settle import ModelSettle


def model(accel=.5):
  return NS(action=NS(desiredCurvature=accel/100), orientationRate=NS(t=[0., .5, 1.], z=[accel/10]*3),
            meta=NS(laneChangeState='off'))


def tick(gate, i, **kw):
  args = dict(model=model(), now=1+i*.05, received=1+i*.05, stamp=i+1, valid=True, speed=10., measured_curvature=.005)
  args.update(kw)
  return gate.update(**args)


def test_fresh_curved_plan_requires_full_dwell():
  gate = ModelSettle()
  for i in range(6):
    assert not tick(gate, i)
  assert tick(gate, 6)


@pytest.mark.parametrize('bad', [dict(valid=False), dict(received=0.), dict(received=100.),
  dict(model=None), dict(measured_curvature=.02), dict(speed=0.), dict(speed=float('nan')),
  dict(model=model(float('nan'))), dict(model=model(3.))])
def test_invalid_or_disagreeing_data_resets_dwell(bad):
  gate = ModelSettle()
  for i in range(6):
    assert not tick(gate, i)
  assert not tick(gate, 6, **bad)
  for i in range(7, 13):
    assert not tick(gate, i)
  assert tick(gate, 13)


def test_duplicates_stall_and_slow_drift_never_satisfy_dwell():
  gate = ModelSettle()
  assert not tick(gate, 0)
  for i in range(100):
    assert not tick(gate, 0, now=1+i*.01)
  gate.reset()
  for i in range(40):
    accel = .5+(i % 20)*.08
    assert not tick(gate, i, model=model(accel), measured_curvature=accel/100)


def test_configured_delay_and_missing_frames():
  gate = ModelSettle()
  for i in range(40):
    assert not tick(gate, i, delay=2.)
  assert tick(gate, 40, delay=2.)
  gate.reset()
  for i in range(5):
    assert not tick(gate, i)
  assert not tick(gate, 20)  # gap restarts the dwell
  for i in range(21, 26):
    assert not tick(gate, i)
  assert tick(gate, 26)


def test_ten_second_user_delay_is_not_shortened():
  gate = ModelSettle()
  for i in range(200):
    assert not tick(gate, i, delay=10.)
  assert tick(gate, 200, delay=10.)


@pytest.mark.parametrize('field,value', [('laneChangeState','laneChangeStarting'), ('t',[0.,.5,.4]), ('z',[0.,float('nan'),0.])])
def test_maneuver_or_corrupt_trajectory_cannot_unlock(field, value):
  m=model()
  setattr(m.meta if field=='laneChangeState' else m.orientationRate, field, value)
  gate=ModelSettle()
  for i in range(30):
    assert not tick(gate,i,model=m)


@pytest.mark.parametrize('direction', [-1, 1])
def test_coherent_gentle_curve_transition_unlocks_at_minimum_dwell(direction):
  gate = ModelSettle()
  for i in range(7):
    # Every frame predicts the same evolving turn. Neither the future path nor
    # the current wheel is straight, and the one-second horizon changes by .7.
    accel = direction * (0.2 + .7*i*.05)
    m = model(accel)
    m.orientationRate.z = [(accel+direction*.7*t)/10 for t in m.orientationRate.t]
    ready = tick(gate, i, model=m, measured_curvature=accel/100)
    assert ready == (i == 6)


def test_abrupt_future_turn_and_action_path_disagreement_do_not_unlock():
  for rates, action in (([.0, .08, .16], .0), ([.0]*3, .8)):
    gate = ModelSettle()
    m = model(action)
    m.orientationRate.z = rates
    for i in range(30):
      assert not tick(gate, i, model=m, measured_curvature=action/100)
