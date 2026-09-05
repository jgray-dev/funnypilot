"""Run selfdrived's real process gate without importing native IPC extensions."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def process_gate(mocker):
  path = ROOT / 'selfdrive/selfdrived/selfdrived.py'
  tree = ast.parse(path.read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SelfdriveD')
  init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
  ignored = next(n for n in init.body if isinstance(n, ast.Assign) and
                 any(isinstance(t, ast.Attribute) and t.attr == 'ignored_processes' for t in n.targets))
  update = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'update_events')
  start = next(i for i, n in enumerate(update.body) if isinstance(n, ast.Assign) and
               any(isinstance(t, ast.Name) and t.id == 'not_running' for t in n.targets))
  # Include the actual logging and gate, including its camera-health else arm.
  body = update.body[start:start + 3]
  assert len(body) == 3 and isinstance(body[-1], ast.If) and body[-1].orelse
  module = ast.Module(body=[ignored, *body], type_ignores=[])
  code = compile(ast.fix_missing_locations(module), str(path), 'exec')

  def run(failed, received=1, cameras_alive=True):
    class Signals(dict):
      recv_frame = {'managerState':received}

      def all_alive(self, _):
        return cameras_alive

      def all_freq_ok(self, _):
        return True

    sm = Signals(managerState=NS(processes=[NS(name=name, running=False, shouldBeRunning=True) for name in failed]))
    state = NS(sm=sm, not_running_prev=None, events=set(), rk=NS(lagging=False), camera_packets=['roadCameraState'])
    log = mocker.Mock()
    scope = {'self':state, 'cloudlog':log, 'SIMULATION':False,
             'EventName':NS(processNotRunning='processNotRunning', cameraMalfunction='cameraMalfunction', cameraFrameRate='cameraFrameRate')}
    exec(code, scope)
    return state, log
  return run


def test_recorder_loss_is_logged_but_does_not_block(process_gate):
  state, log = process_gate(['funnypilot_feedback'])
  assert state.events == set()
  log.event.assert_called_once_with('process_not_running', not_running={'funnypilot_feedback'}, error=True)
  assert state.ignored_processes == {'mapd', 'funnypilot_feedback', 'funnypilot_astra'}


@pytest.mark.parametrize('required', ['controlsd', 'calibrationd', 'plannerd', 'modeld', 'camerad', 'pandad', 'radard', 'feedbackd'])
@pytest.mark.parametrize('optional_failed', [[], ['funnypilot_feedback'], ['funnypilot_astra'], ['funnypilot_feedback', 'funnypilot_astra']])
def test_every_driving_process_still_blocks(process_gate, required, optional_failed):
  failed = [required] + optional_failed
  state, _ = process_gate(failed)
  assert 'processNotRunning' in state.events


@pytest.mark.parametrize('failed', [['funnypilot_feedback'], ['funnypilot_astra'], ['funnypilot_feedback', 'funnypilot_astra']])
def test_optional_failure_cannot_hide_camera_health(process_gate, failed):
  state, _ = process_gate(failed, cameras_alive=False)
  assert state.events == {'cameraMalfunction'}


@pytest.mark.parametrize('failed', [['funnypilot_astra'], ['funnypilot_feedback', 'funnypilot_astra']])
def test_astra_failure_is_optional_and_logged(process_gate, failed):
  state, log = process_gate(failed)
  assert not state.events
  log.event.assert_called_once_with('process_not_running', not_running=set(failed), error=True)


def test_no_process_verdict_before_first_manager_message(process_gate):
  state, log = process_gate(['controlsd'], received=0)
  assert not state.events
  log.event.assert_not_called()
