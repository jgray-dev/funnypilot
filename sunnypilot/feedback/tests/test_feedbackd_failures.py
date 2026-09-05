"""Recorder IO failures are retryable, bounded, and never false success."""
import ast
import errno
from functools import partial
import json
from pathlib import Path
import socket
from types import SimpleNamespace as NS

import pytest

from openpilot.sunnypilot.feedback import feedbackd as D
from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.feedback.capture import Capture

ID = 'b' * 32
ROUTE = '2026-09-05--10-00-00'


def unavailable(*args, **kwargs):
  raise OSError(errno.ENOSPC, 'No space left on device')


@pytest.fixture
def loop(tmp_path, monkeypatch, mocker):
  log = tmp_path / 'logs'
  (log / (ROUTE + '--0')).mkdir(parents=True)
  for name in ('SOCKET', 'STATUS', 'CORNER_RULES', 'CORNER_CONTEXT'):
    monkeypatch.setattr(P, name, str(tmp_path / name))
  monkeypatch.setattr(D, 'Capture', partial(Capture, root=str(tmp_path / 'capture')))
  runner = D.CaptureLoop(str(log), {'commit':'fixture'}, mocker.Mock())
  yield runner
  runner.close()


def send(now=101, **changes):
  cmd = {'id':ID, 'labels':['steering_bite'], 'started':100, 'sent':now} | changes
  with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
    client.sendto(json.dumps(cmd).encode(), P.SOCKET)


def test_startup_storage_retries_without_busy_loop(loop, monkeypatch, mocker):
  factory = D.Capture
  broken = mocker.Mock(side_effect=unavailable)
  monkeypatch.setattr(D, 'Capture', broken)
  loop.tick(100, None, lambda: ROUTE)
  assert loop.capture is None and loop.sock is None
  for now in (100.01, 101, 104.99):
    loop.tick(now, None, lambda: ROUTE)
  assert broken.call_count == 1
  assert loop.logger.exception.call_count == 1
  monkeypatch.setattr(D, 'Capture', factory)
  loop.tick(100 + D.RETRY_S, None, lambda: ROUTE)
  assert loop.capture is not None and loop.sock is not None


def test_socket_setup_closes_failed_descriptor_and_retries(loop, monkeypatch, mocker):
  bind = socket.socket.bind
  seen = []

  def denied(sock, address):
    seen.append(sock)
    raise PermissionError(errno.EACCES, 'Permission denied')

  monkeypatch.setattr(socket.socket, 'bind', denied)
  loop.tick(100, None, lambda: ROUTE)
  assert seen[0].fileno() == -1
  capture = loop.capture
  assert capture is not None and loop.sock is None
  loop.tick(104, None, lambda: ROUTE)
  assert len(seen) == 1
  monkeypatch.setattr(socket.socket, 'bind', bind)
  loop.tick(105, None, lambda: ROUTE)
  assert loop.capture is capture and loop.sock is not None


def test_finalization_failure_preserves_capture_and_limits_retries(loop, monkeypatch, mocker):
  loop.tick(100, None, lambda: ROUTE)
  send()
  loop.tick(101, lambda: {'t':101, 'angle':1.0}, lambda: ROUTE)
  capture = loop.capture
  assert ID in capture.active
  finish = capture.finish
  broken = mocker.Mock(side_effect=unavailable)
  monkeypatch.setattr(capture, 'finish', broken)
  loop.tick(121, None, lambda: ROUTE)
  assert capture.active[ID]['state'] == 'capturing'
  assert capture.active[ID]['capture_interrupted']
  for now in (121.01, 122, 125.99):
    loop.tick(now, lambda now=now: {'t':now}, lambda: ROUTE)
  assert broken.call_count == 1
  monkeypatch.setattr(capture, 'finish', finish)
  loop.tick(126, None, lambda: ROUTE)
  assert loop.capture is capture and ID not in capture.active
  event = P.read_json(str(capture.events / ID / 'event.json'))
  assert event['state'] == 'queued' and event['capture_interrupted']


def test_report_and_failure_status_can_both_fail(loop, monkeypatch, mocker):
  loop.tick(100, None, lambda: ROUTE)
  send()
  write = P.atomic_json
  broken = mocker.Mock(side_effect=unavailable)
  monkeypatch.setattr(P, 'atomic_json', broken)
  loop.tick(101, None, lambda: ROUTE)
  assert not Path(P.STATUS).exists()
  assert loop.retry_at == 106 and loop.logger.exception.call_count == 1
  calls = broken.call_count
  loop.tick(105, None, lambda: ROUTE)
  assert broken.call_count == calls
  monkeypatch.setattr(P, 'atomic_json', write)
  # No new UI packet: retry the already accepted request even after its input
  # freshness window expires. A newly arriving stale command still cannot save.
  loop.tick(130, None, lambda: ROUTE)
  assert loop.pending_report is None
  assert P.read_json(P.STATUS)['saved']
  assert P.read_json(str(loop.capture.events / ID / 'event.json'))['labels'] == ['steering_bite']


def test_bad_command_status_failure_is_best_effort(loop, monkeypatch, mocker):
  loop.tick(100, None, lambda: ROUTE)
  send(labels=['not a label'])
  monkeypatch.setattr(P, 'atomic_json', unavailable)
  loop.tick(101, None, lambda: ROUTE)
  assert not loop.capture.active
  assert not Path(P.STATUS).exists()


def test_nonfinite_sample_backoff_and_recovery(loop):
  loop.tick(100, None, lambda: ROUTE)
  send()
  loop.tick(101, None, lambda: ROUTE)
  loop.tick(102, lambda: {'t':102, 'angle':float('nan')}, lambda: ROUTE)
  assert loop.capture.active[ID]['capture_interrupted']
  assert loop.retry_at == 107
  loop.tick(107, lambda: {'t':107, 'angle':1.0}, lambda: ROUTE)
  assert loop.capture.ring[-1]['t'] == 107


def test_main_keeps_running_without_niceness_and_closes_socket(loop, monkeypatch, mocker):
  # Execute main's real body, replacing only its device-only imports. This
  # checks that the tested IO boundary is actually driven by the shipped loop.
  tree = ast.parse(Path(D.__file__).read_text())
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  main.body = [n for n in main.body if not isinstance(n, (ast.Import, ast.ImportFrom))]

  class Signals(dict):
    valid = alive = {'deviceState':True}
    updated = {'carState':False}
    update = mocker.Mock(side_effect=[None, KeyboardInterrupt()])

  sm = Signals(deviceState=NS(started=True, networkType=0))
  monkeypatch.setattr(D.os, 'nice', mocker.Mock(side_effect=PermissionError('priority')))
  scope = vars(D) | {'messaging':NS(SubMaster=lambda *a, **kw: sm),
                     'log':NS(DeviceState=NS(NetworkType=NS(wifi=1))),
                     'Params':lambda: NS(get=lambda _: ROUTE), 'Paths':NS(log_root=lambda: loop.log_root),
                     'cloudlog':loop.logger, 'CaptureLoop':lambda *a: loop,
                     'identity':lambda _: {}, 'time':NS(monotonic=lambda: 100)}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[main], type_ignores=[])), D.__file__, 'exec'), scope)
  with pytest.raises(KeyboardInterrupt):
    scope['main']()
  assert loop.capture is not None and loop.sock.fileno() == -1
  loop.logger.exception.assert_called_once_with('feedback scheduling priority unavailable')


def test_unexpected_programming_error_remains_visible(loop, monkeypatch, mocker):
  loop.tick(100, None, lambda: ROUTE)
  monkeypatch.setattr(loop.capture, 'finish', mocker.Mock(side_effect=AssertionError('defect')))
  with pytest.raises(AssertionError, match='defect'):
    loop.tick(101, None, lambda: ROUTE)
