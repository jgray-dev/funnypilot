"""card's /dev/shm carState mirror: fresh once, stale never, garbage never."""
import json
from types import SimpleNamespace as NS

import pytest

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.feedback import carstate_shm as C
from openpilot.sunnypilot.feedback.feedbackd import snapshot


def car_state(**changes):
  base = dict(canValid=True, vEgo=12.5, vEgoRaw=12.4, aEgo=-0.25, steeringAngleDeg=-3.125, steeringTorque=-42.0,
              steeringPressed=True, brakePressed=False, gasPressed=False, standstill=False)
  return NS(**(base | changes))


@pytest.fixture
def channel(tmp_path):
  path = str(tmp_path / 'fp_carstate')
  return C.CarStateTapPublisher(path), C.CarStateTapReader(path)


def test_round_trip_preserves_every_field(channel):
  writer, reader = channel
  writer.update(car_state(), mono=100.0)
  rec = reader.poll(100.05)
  assert rec == {'observed':100.0, 'can_valid':True, 'v_ego':12.5, 'v_ego_raw':12.4, 'a_ego':-0.25,
                 'steering_angle_deg':-3.125, 'steering_torque':-42.0, 'steering_pressed':True,
                 'brake_pressed':False, 'gas_pressed':False, 'standstill':False}
  assert not (writer.tmp and __import__('os').path.exists(writer.tmp))


def test_stale_record_is_never_a_sample_but_stays_latest(channel):
  writer, reader = channel
  writer.update(car_state(standstill=True, vEgo=0.0, vEgoRaw=0.0), mono=100.0)
  assert reader.poll(100.0 + C.STALE_S + 0.01) is None
  assert reader.poll(99.0 - 1.5) is None  # a stamp from the future
  assert reader.latest['observed'] == 100.0
  assert reader.poll(100.1) is not None  # fresh again for a reader whose clock is inside the window


@pytest.mark.parametrize('raw', ['', 'fpcs0,1,1,1,1,1,1,1,1,1,1,1', 'fpcs1,1,1,1,1,1,1,1,1,1,1', 'fpcs1,1,1,1,1,1,1,1,1,1,1,1,1',
                                 'fpcs1,nan,1,1,1,1,1,1,1,1,1,1', 'fpcs1,1,2,1,1,1,1,1,1,1,1,1', 'fpcs1,1,1,inf,1,1,1,1,1,1,1,1',
                                 'fpcs1,1,x,1,1,1,1,1,1,1,1,1'])
def test_garbage_reads_as_nothing(channel, raw):
  writer, reader = channel
  with open(writer.path, 'w') as f:
    f.write(raw)
  assert reader.poll(1.0) is None and reader.latest is None


def test_missing_file_and_writer_failure_are_silent(tmp_path):
  reader = C.CarStateTapReader(str(tmp_path / 'absent'))
  assert reader.poll(1.0) is None
  writer = C.CarStateTapPublisher(str(tmp_path / 'no-such-dir' / 'fp_carstate'))
  writer.update(car_state())  # must not raise into card
  writer.update(NS())         # a CarState missing fields must not raise either


def test_publish_motion_uses_the_mirror_window(channel, tmp_path, monkeypatch):
  writer, reader = channel
  monkeypatch.setattr(P, 'MOTION', str(tmp_path / 'motion.json'))
  P.publish_motion(None, 10.0)
  assert json.load(open(P.MOTION)) == {'stationary':None, 'observed':0.0}
  writer.update(car_state(standstill=True, vEgo=0.0, vEgoRaw=0.0), mono=100.0)
  reader.poll(100.0 + C.STALE_S + 1.0)  # stale for sampling, still inside publish_motion's 2 s window
  P.publish_motion(reader.latest, 101.5)
  assert json.load(open(P.MOTION)) == {'stationary':True, 'observed':100.0}
  P.publish_motion(reader.latest, 102.5)
  assert json.load(open(P.MOTION))['stationary'] is None
  writer.update(car_state(canValid=False, standstill=True, vEgo=0.0, vEgoRaw=0.0), mono=200.0)
  reader.poll(200.0)
  P.publish_motion(reader.latest, 200.5)
  assert json.load(open(P.MOTION)) == {'stationary':None, 'observed':200.0}


def test_snapshot_without_car_is_visibly_incomplete():
  from cereal import log, custom, car
  sm = {'controlsState':log.ControlsState.new_message(), 'longitudinalPlan':log.LongitudinalPlan.new_message(),
        'liveTorqueParameters':log.LiveTorqueParametersData.new_message(), 'carControl':car.CarControl.new_message(),
        'radarState':log.RadarState.new_message(), 'longitudinalPlanSP':custom.LongitudinalPlanSP.new_message(),
        'selfdriveState':log.SelfdriveState.new_message(), 'liveCalibration':log.LiveCalibrationData.new_message(),
        'onroadEvents':[], 'onroadEventsSP':custom.OnroadEventSP.new_message()}

  class Signals(dict):
    valid = dict.fromkeys(sm, True)
    alive = valid

  row = snapshot(Signals(sm), None, 5.0)
  assert row['v'] is None and row['driver_torque'] is None and row['t_car'] is None
  assert row['valid']['carState'] is False and row['valid']['controlsState'] is True
  json.dumps(row, allow_nan=False)
