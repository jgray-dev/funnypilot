"""A blocked diagnostic disk must not stall the production publishers."""
import ast
import threading
from pathlib import Path

from openpilot.selfdrive.controls.lib.triage_recorder import AsyncTriageRecorder, TriageRecorder


def test_blocked_disk_keeps_caller_nonblocking_and_queue_bounded(monkeypatch):
  entered, release, finished = threading.Event(), threading.Event(), threading.Event()
  rows = []

  def blocked_write(self, row):
    entered.set()
    release.wait(5)
    rows.append(row)
    return True

  monkeypatch.setattr(TriageRecorder, 'write', blocked_write)
  recorder = AsyncTriageRecorder('blocked')
  record = {'nested': {'value': 1}}
  assert recorder.write(record)
  assert entered.wait(1)
  record['nested']['value'] = 99

  def producer():
    for i in range(100):
      recorder.write({'i': i})
    finished.set()

  caller = threading.Thread(target=producer)
  caller.start()
  try:
    assert finished.wait(1), 'diagnostic IO blocked the caller'
    assert recorder.queue.qsize() == 32
    assert recorder.dropped == 68
  finally:
    release.set()
    caller.join(2)
    recorder.queue.join()
  assert rows[0]['nested']['value'] == 1
  assert [r['i'] for r in rows[1:]] == list(range(32))


def test_worker_survives_failed_write(monkeypatch):
  rows = []

  def failing_write(self, row):
    if row['i'] == 0:
      raise OSError('disk failure')
    rows.append(row)
    return True

  monkeypatch.setattr(TriageRecorder, 'write', failing_write)
  recorder = AsyncTriageRecorder('failure')
  recorder.write({'i': 0})
  recorder.write({'i': 1})
  recorder.queue.join()
  assert recorder.dropped == 1
  assert [r['i'] for r in rows] == [1]


def test_control_and_radar_use_async_diagnostic_writer():
  root = Path(__file__).resolve().parents[4]
  for file in ('controlsd.py', 'radard.py'):
    tree = ast.parse((root / 'selfdrive/controls' / file).read_text())
    calls = [n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert 'AsyncTriageRecorder' in calls
    assert 'TriageRecorder' not in calls
