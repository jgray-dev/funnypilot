import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.feedback.capture import Capture
from openpilot.sunnypilot.feedback.corner_feedback import corrected_target
from openpilot.sunnypilot.feedback.feedbackd import snapshot
from openpilot.system.loggerd.drive_retention import read_saved

ID = 'a' * 32
ROUTE = '2026-09-05--10-00-00'


@pytest.fixture
def capture(tmp_path, monkeypatch):
  log = tmp_path / 'logs'
  (log / (ROUTE+'--0')).mkdir(parents=True)
  for name in ('STATUS','CORNER_CONTEXT','CORNER_RULES'):
    monkeypatch.setattr(P, name, str(tmp_path / name))
  cap = Capture(str(tmp_path / 'feedback'), str(log), {'commit':'123abcd'})
  return cap


def report(cap, labels, now=101):
  return cap.report({'id':ID,'labels':labels,'started':100,'sent':now}, ROUTE, now)


def context(governing=True, **kwargs):
  P.atomic_json(P.CORNER_CONTEXT, dict(t=101, governing=governing, lat=38.0, lon=-77.0, bearing=90., unmanageable=False) | kwargs)


def test_capture_protects_route_and_all_labels_immediately(capture):
  with pytest.raises(ValueError):
    capture.sample({'t':80, 'angle':float('nan')})
  for t in (70,81,99,100):
    capture.sample({'t':t,'angle':1.0})
  event = report(capture,['steering_bite'])
  assert read_saved(capture.log_root) == {ROUTE}
  assert event['labels'] == ['steering_bite']
  report(capture,['late_braking'],102)
  capture.sample({'t':110,'angle':2.0})
  capture.finish(121)
  saved = P.read_json(str(capture.events / ID / 'event.json'))
  assert saved['state'] == 'queued' and saved['labels'] == ['late_braking','steering_bite']
  rows = [json.loads(l) for l in (capture.events / ID / 'telemetry.jsonl').read_text().splitlines()]
  assert [r['t'] for r in rows] == [81,99,100,110]


def test_unnecessary_map_slowdown_persists_exact_corner(capture):
  context()
  event = report(capture,['unnecessary_slowdown'])
  assert 'relief saved' in event['remedy']
  rules = P.read_json(str(capture.root / 'corners.json'))['rules']
  corner=NS(lat=38.,lon=-77.,bearing=90.,v_target=15.,unmanageable=False)
  target=corrected_target(corner,25.,rules)
  assert target == pytest.approx(15.45)
  for invalid_cruise in (float('nan'), float('inf'), float('-inf')):
    assert corrected_target(corner, invalid_cruise, rules) == 15.
  report(capture,['unnecessary_slowdown'],102)
  assert len(capture.rules['rules']) == 1
  assert corrected_target(corner,25.,capture.rules['rules']) == target
  # Nearby opposite carriageway and a stressed corner never inherit relief.
  corner.bearing=270.
  assert corrected_target(corner,25.,rules) == 15.
  corner.bearing=90.
  corner.unmanageable=True
  assert corrected_target(corner,25.,rules) == 15.


@pytest.mark.parametrize('overrides', [{'governing':False},{'unmanageable':True},{'t':0},{'lat':float('nan')}])
def test_other_sources_stress_and_stale_context_do_not_relax_map(capture, overrides):
  if 'lat' in overrides:
    Path(P.CORNER_CONTEXT).write_text(json.dumps(dict(t=101,governing=True,lat=float('nan'),lon=-77.,bearing=90.)))
  else:
    context(**overrides)
  event=report(capture,['unnecessary_slowdown'])
  assert 'no eligible' in event['remedy']
  assert capture.rules['rules'] == []


def test_new_label_uses_bookmark_corner_not_a_different_corner(capture):
  context()
  report(capture,[])
  context(lat=40.)
  report(capture,['unnecessary_slowdown'],110)
  assert capture.rules['rules'][0]['lat'] == 38.


def test_restart_keeps_captured_data_and_queues_interrupted_report(capture):
  report(capture,['steering_wander'])
  Capture(str(capture.root),capture.log_root)
  event=P.read_json(str(capture.events / ID / 'event.json'))
  assert event['state']=='queued' and event['capture_interrupted']
  assert read_saved(capture.log_root)=={ROUTE}


def test_late_or_malformed_commands_do_not_save(capture):
  for cmd in ({'id':'../escape'}, {'id':ID,'labels':['bad'],'started':100,'sent':101},
              {'id':ID,'labels':[],'started':1,'sent':101}):
    with pytest.raises(ValueError):
      capture.report(cmd,ROUTE,101)
  assert not read_saved(capture.log_root)


def test_snapshot_uses_real_cereal_fields():
  from cereal import car, log, custom
  sm={'carState':car.CarState.new_message(), 'controlsState':log.ControlsState.new_message(),
      'longitudinalPlan':log.LongitudinalPlan.new_message(), 'liveTorqueParameters':log.LiveTorqueParametersData.new_message(),
      'carControl':car.CarControl.new_message(), 'radarState':log.RadarState.new_message(),
      'longitudinalPlanSP':custom.LongitudinalPlanSP.new_message(),
      'selfdriveState':log.SelfdriveState.new_message(), 'liveCalibration':log.LiveCalibrationData.new_message(),
      'onroadEvents':[log.OnroadEvent.new_message(name='selfdriveInitializing',noEntry=True)],
      'onroadEventsSP':custom.OnroadEventSP.new_message(events=[{'name':'silentBrakeHold','noEntry':True}])}
  sm['selfdriveState'].alertType = 'calibrationIncomplete/noEntry'
  sm['selfdriveState'].alertText2 = 'Calibration in Progress'
  sm['liveCalibration'].calStatus = 'uncalibrated'
  sm['controlsState'].lateralControlState.init('torqueState')
  class Signals(dict):
    valid=dict.fromkeys(sm, True)
    alive=valid
  result=snapshot(Signals(sm),100)
  assert result['desired_curvature']==0 and 'lateral' in result
  assert result['selfdrive']['alert_type'] == 'calibrationIncomplete/noEntry'
  assert result['calibration']['status'] == 'uncalibrated'
  assert result['blocking_events'] == {'onroadEvents':['selfdriveInitializing'], 'onroadEventsSP':['silentBrakeHold']}
  json.dumps(result,allow_nan=False)


def test_map_cap_gas_gate_and_ribbon_use_one_bounded_target():
  from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_map_v2 import SCCMapV2, TrackedCorner, GATE_LEAD_T, GATE_V_MARGIN
  from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import corner_speed as C
  scc=SCCMapV2(params=NS(get_bool=lambda _:True),route_reader=list)
  corner=TrackedCorner(38.,-77.,90.,100.,40.,0.,2.25,3,1.,15.,1,45.)
  scc.corners=[corner]
  scc._feedback.rules=[{'lat':38.,'lon':-77.,'bearing':90.}]
  corrected=corrected_target(corner,25.,scc._feedback.rules)
  assert scc._raw_cap(25.) == pytest.approx(C.corner_cap(corrected,0.,40.))
  assert scc.display_corners(25.)[0].v_target == corrected
  assert corner.v_target == 15.  # no ratchet through mutable geometry
  scc.is_enabled=True
  speed=15.2+GATE_V_MARGIN
  corner.distance=speed*GATE_LEAD_T
  scc._update_gas_gate(speed,25.)
  with_feedback=scc.gas_gating_active
  scc._feedback.rules=[]
  scc.gas_gating_active=False
  scc._update_gas_gate(speed,25.)
  assert scc.gas_gating_active and not with_feedback
