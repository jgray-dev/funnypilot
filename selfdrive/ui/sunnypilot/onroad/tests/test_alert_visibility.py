"""Execute the production alert selector with real messages; no native UI needed."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from cereal import log
from openpilot.selfdrive.ui.onroad.model_status import model_overlay_ready

ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def selector():
  tree = ast.parse((ROOT / 'selfdrive/ui/onroad/alert_renderer.py').read_text())
  quiet = next(n.value.args[0] for n in tree.body if isinstance(n, ast.Assign) and
               any(isinstance(t, ast.Name) and t.id == 'QUIET_ALERT_TYPES' for t in n.targets))
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'get_alert')
  # Only graphics/IPC surroundings are substituted. Run the method from disk,
  # including its stale-message and watchdog paths, not a copy of the filter.
  scope = {'log':log, 'Alert':NS, 'AlertStatus':log.SelfdriveState.AlertStatus,
           'AlertSize':log.SelfdriveState.AlertSize, 'QUIET_ALERT_TYPES':frozenset(ast.literal_eval(quiet)),
           'gui_app':NS(sunnypilot_ui=lambda:True), 'ui_state':NS(started_frame=10, started_time=90),
           'time':NS(monotonic=lambda:100), 'TICI':True,
           'SELFDRIVE_STATE_TIMEOUT':5, 'SELFDRIVE_UNRESPONSIVE_TIMEOUT':10,
           'ALERT_STARTUP_PENDING':'startup', 'ALERT_CRITICAL_TIMEOUT':'takeover', 'ALERT_CRITICAL_REBOOT':'unresponsive'}
  unit = ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),method],type_ignores=[])
  exec(compile(ast.fix_missing_locations(unit), '<production get_alert>', 'exec'), scope)
  return scope


class Signals(dict):
  def __init__(self, **messages):
    super().__init__(messages)
    self.valid = dict.fromkeys(messages, True)
    self.alive = dict.fromkeys(messages, True)
    self.updated = dict.fromkeys(messages, True)
    self.recv_frame = dict.fromkeys(messages, 20)
    self.recv_time = dict.fromkeys(messages, 100.)


def signal(kind, status='normal', size='mid'):
  state = log.SelfdriveState.new_message(alertType=kind, alertStatus=status, alertSize=size,
                                       alertText1='openpilot Unavailable', alertText2='Specific reason')
  return Signals(selfdriveState=state)


@pytest.mark.parametrize('kind', [
  'calibrationIncomplete/noEntry', 'calibrationIncomplete/permanent',
  'calibrationInvalid/permanent', 'calibrationRecalibrating/permanent',
  'commIssue/noEntry', 'processNotRunning/noEntry', 'controlsInitializing/noEntry',
  'startupNoCar/permanent', 'startupNoControl/permanent', 'futureFault/permanent',
  'laneChange/noEntry', 'cameraMalfunction/permanent',
])
def test_refusals_calibration_and_unknown_faults_are_visible(selector, kind):
  alert = selector['get_alert'](None, signal(kind))
  assert alert.text1 == 'openpilot Unavailable' and alert.text2 == 'Specific reason'


def test_routine_notices_stay_quiet_but_never_hide_raised_severity(selector):
  for kind in selector['QUIET_ALERT_TYPES']:
    assert selector['get_alert'](None, signal(kind)) is None
    for status, size in [('userPrompt','mid'), ('critical','full'), ('normal','full')]:
      assert selector['get_alert'](None, signal(kind,status,size)) is not None
  selector['gui_app'].sunnypilot_ui = lambda:False
  assert selector['get_alert'](None, signal('laneChange/warning')) is not None


def test_stale_alert_and_process_loss_paths_still_work(selector):
  sm = signal('calibrationInvalid/noEntry')
  sm.recv_frame['selfdriveState'] = 0
  assert selector['get_alert'](None,sm) is None
  sm.updated['selfdriveState'] = False
  assert selector['get_alert'](None,sm) == 'startup'
  sm.recv_frame['selfdriveState'] = 20
  sm.recv_time['selfdriveState'] = 94
  sm['selfdriveState'].enabled = True
  assert selector['get_alert'](None,sm) == 'takeover'
  sm['selfdriveState'].enabled = False
  assert selector['get_alert'](None,sm) == 'unresponsive'


def test_overlay_withdraws_on_bad_calibration_and_recovers_with_fresh_data():
  calib = log.LiveCalibrationData.new_message(calStatus='calibrated',rpyCalib=[0,.02,-.03])
  sm = Signals(liveCalibration=calib,modelV2=log.ModelDataV2.new_message())
  assert model_overlay_ready(sm,10)
  for status in ('uncalibrated','recalibrating','invalid'):
    calib.calStatus = status
    assert not model_overlay_ready(sm,10)
  calib.calStatus = 'calibrated'
  for field in ('valid','alive'):
    for service in sm:
      getattr(sm,field)[service] = False
      assert not model_overlay_ready(sm,10)
      getattr(sm,field)[service] = True
  for service in sm:
    sm.recv_frame[service] = 9
    assert not model_overlay_ready(sm,10)
    sm.recv_frame[service] = 20
  calib.rpyCalib = [0,float('nan'),0]
  assert not model_overlay_ready(sm,10)
  calib.rpyCalib = [0,.02,-.03]
  assert model_overlay_ready(sm,10)


def test_readiness_gates_both_model_and_follow_distance_projection():
  tree = ast.parse((ROOT/'selfdrive/ui/onroad/augmented_road_view.py').read_text())
  gates = [n for n in ast.walk(tree) if isinstance(n,ast.If) and
           any(isinstance(x,ast.Name) and x.id=='geometry_ready' for x in ast.walk(n.test))]
  bodies = '\n'.join(ast.unparse(n) for gate in gates for n in gate.body)
  assert 'self.model_renderer.render(' in bodies
  assert 'self._follow_line.draw' in bodies
