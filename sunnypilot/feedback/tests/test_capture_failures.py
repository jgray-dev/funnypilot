"""Storage faults exercise the real filesystem and the same Capture on retry."""
import builtins
import errno
import json
import os
import stat
from pathlib import Path

import pytest

from openpilot.sunnypilot.feedback import capture as C
from openpilot.sunnypilot.feedback import protocol as P
from openpilot.system.loggerd.drive_retention import read_saved

ID = 'a' * 32
ROUTE = '2026-09-05--10-00-00'


@pytest.fixture
def cap(tmp_path, monkeypatch):
  logs = tmp_path / 'logs'
  (logs / (ROUTE + '--0')).mkdir(parents=True)
  for name in ('STATUS', 'CORNER_CONTEXT', 'CORNER_RULES'):
    monkeypatch.setattr(P, name, str(tmp_path / name))
  capture = C.Capture(tmp_path / 'feedback', str(logs))
  P.atomic_json(P.CORNER_CONTEXT, dict(t=101, governing=True, lat=38., lon=-77., bearing=90., unmanageable=False))
  capture.sample({'t':99, 'angle':1.})
  return capture


def report(cap, labels=()):
  return cap.report(dict(id=ID, labels=list(labels), started=100, sent=101), ROUTE, 101)


def no_space(*args, **kwargs):
  raise OSError(errno.ENOSPC, 'injected full storage')


@pytest.mark.parametrize('target', ['corners.json', 'CORNER_RULES', 'event.json'])
def test_rule_and_label_failures_retry_without_false_success(cap, monkeypatch, target):
  report(cap, ['steering_bite'])
  original = P.atomic_json
  def fail(path, *args, **kwargs):
    if Path(path).name == target:
      no_space()
    return original(path, *args, **kwargs)
  with monkeypatch.context() as fault:
    fault.setattr(P, 'atomic_json', fail)
    with pytest.raises(OSError, match='full storage'):
      report(cap, ['unnecessary_slowdown'])
  assert cap.active[ID]['labels'] == ['steering_bite']
  assert P.read_json(str(cap.events / ID / 'event.json'))['labels'] == ['steering_bite']
  if target != 'event.json':
    assert cap.rules['rules'] == []
  event = report(cap, ['unnecessary_slowdown'])
  assert event['labels'] == ['steering_bite', 'unnecessary_slowdown']
  assert 'relief saved' in event['remedy']
  assert len(cap.rules['rules']) == 1
  assert P.read_json(str(cap.root / 'corners.json')) == cap.rules == P.read_json(P.CORNER_RULES)
  report(cap, ['unnecessary_slowdown'])
  assert len(cap.rules['rules']) == 1


@pytest.mark.parametrize('phase', ['mkdir', 'telemetry', 'metadata', 'metadata_directory_fsync'])
def test_initial_report_failure_retries_its_directory_and_original_corner(cap, monkeypatch, phase):
  original_open, original_json, original_mkdir = builtins.open, P.atomic_json, Path.mkdir
  def fail_open(path, *args, **kwargs):
    if Path(path).name == 'telemetry.jsonl.tmp':
      no_space()
    return original_open(path, *args, **kwargs)
  def fail_json(path, *args, **kwargs):
    if Path(path).name == 'event.json':
      if phase == 'metadata_directory_fsync':
        original_json(path, *args, **kwargs)  # rename landed, durability acknowledgement failed
      no_space()
    return original_json(path, *args, **kwargs)
  def fail_mkdir(path, *args, **kwargs):
    if path.name == ID:
      no_space()
    return original_mkdir(path, *args, **kwargs)
  with monkeypatch.context() as fault:
    if phase == 'mkdir':
      fault.setattr(Path, 'mkdir', fail_mkdir)
    elif phase == 'telemetry':
      fault.setattr(C, 'open', fail_open, raising=False)
    else:
      fault.setattr(P, 'atomic_json', fail_json)
    with pytest.raises(OSError):
      report(cap, ['unnecessary_slowdown'])
  assert cap.active[ID]['labels'] == []
  assert read_saved(cap.log_root) == {ROUTE}
  P.atomic_json(P.CORNER_CONTEXT, dict(t=101, governing=True, lat=40., lon=-77., bearing=90.))
  report(cap, ['unnecessary_slowdown'])
  assert cap.active[ID]['corner']['lat'] == 38.
  assert len(cap.rules['rules']) == 1
  assert json.loads((cap.events / ID / 'telemetry.jsonl').read_text())['t'] == 99


def test_metadata_retry_does_not_truncate_post_event_samples(cap, monkeypatch):
  original = P.atomic_json
  def fail(path, *args, **kwargs):
    if Path(path).name == 'event.json':
      no_space()
    return original(path, *args, **kwargs)
  with monkeypatch.context() as fault:
    fault.setattr(P, 'atomic_json', fail)
    with pytest.raises(OSError):
      report(cap)
  cap.sample({'t':110})
  cap.ring.clear()  # future retry must not reconstruct an already collected artifact
  report(cap)
  assert [json.loads(row)['t'] for row in (cap.events / ID / 'telemetry.jsonl').read_text().splitlines()] == [99, 110]


@pytest.mark.parametrize('after_write', [False, True])
def test_final_metadata_failure_keeps_active_candidate_and_collected_triage(cap, monkeypatch, after_write):
  report(cap, ['steering_bite'])
  original = P.atomic_json
  calls = []
  def triage(event):
    calls.append(event['id'])
    with C._artifact(cap.events / ID / 'triage.jsonl') as out:
      out.write('collected diagnostic\n')
  monkeypatch.setattr(cap, '_triage', triage)
  def fail(path, data, **kwargs):
    if Path(path).name == 'event.json' and data['state'] == 'queued':
      if after_write:
        original(path, data, **kwargs)
      no_space()
    return original(path, data, **kwargs)
  with monkeypatch.context() as fault:
    fault.setattr(P, 'atomic_json', fail)
    with pytest.raises(OSError):
      cap.finish(121)
  assert cap.active[ID]['state'] == 'capturing'
  cap.finish(126)
  assert ID not in cap.active
  assert calls == [ID]
  assert P.read_json(str(cap.events / ID / 'event.json'))['state'] == 'queued'
  assert (cap.events / ID / 'triage.jsonl').read_text() == 'collected diagnostic\n'


@pytest.mark.parametrize('fault_stage', ['write', 'fsync'])
def test_failed_triage_preserves_prior_artifact_and_propagates_destination_error(cap, monkeypatch, fault_stage):
  report(cap)
  artifact = cap.events / ID / 'triage.jsonl'
  artifact.write_text('previous diagnostic\n')
  source = cap.root / 'source.jsonl'
  source.write_text(json.dumps({'t':cap.active[ID]['created_at']}) + '\n')
  original = builtins.open
  class BrokenOutput:
    def __enter__(self):
      return self
    def __exit__(self, *args):
      return False
    write = no_space
  def open_file(path, *args, **kwargs):
    if str(path).startswith('/data/funnypilot_triage/'):
      return original(source, *args, **kwargs)
    if Path(path).name == 'triage.jsonl.tmp' and fault_stage == 'write':
      return BrokenOutput()
    return original(path, *args, **kwargs)
  with monkeypatch.context() as fault:
    fault.setattr(C, 'open', open_file, raising=False)
    if fault_stage == 'fsync':
      fault.setattr(C.os, 'fsync', no_space)
    with pytest.raises(OSError):
      cap.finish(121)
  assert artifact.read_text() == 'previous diagnostic\n'
  assert cap.active[ID]['state'] == 'capturing'
  with monkeypatch.context() as retry:
    retry.setattr(C, 'open', lambda path, *a, **kw: original(source if str(path).startswith('/data/funnypilot_triage/') else path, *a, **kw), raising=False)
    cap.finish(126)
  assert ID not in cap.active
  assert 'previous diagnostic' not in artifact.read_text()


def test_missing_triage_sources_are_tolerated_but_other_source_errors_retry(cap, monkeypatch):
  report(cap)
  original = builtins.open
  def fail_source(path, *args, **kwargs):
    if str(path).startswith('/data/funnypilot_triage/'):
      raise PermissionError(errno.EACCES, 'source unreadable')
    return original(path, *args, **kwargs)
  with monkeypatch.context() as fault:
    fault.setattr(C, 'open', fail_source, raising=False)
    with pytest.raises(PermissionError):
      cap.finish(121)
  assert ID in cap.active
  def absent_source(path, *args, **kwargs):
    if str(path).startswith('/data/funnypilot_triage/'):
      raise FileNotFoundError(errno.ENOENT, 'source absent')
    return original(path, *args, **kwargs)
  with monkeypatch.context() as retry:
    retry.setattr(C, 'open', absent_source, raising=False)
    cap.finish(126)
  assert ID not in cap.active
  assert (cap.events / ID / 'triage.jsonl').read_text() == ''


def test_sample_failure_marks_every_event_affected_by_backoff(cap, monkeypatch):
  report(cap)
  second = 'b' * 32
  cap.report(dict(id=second, labels=[], started=100, sent=101), ROUTE, 101)
  original = builtins.open
  def fail_second(path, *args, **kwargs):
    if Path(path).parent.name == second:
      no_space()
    return original(path, *args, **kwargs)
  with monkeypatch.context() as fault:
    fault.setattr(C, 'open', fail_second, raising=False)
    with pytest.raises(OSError):
      cap.sample({'t':110})
  assert all(event['capture_interrupted'] for event in cap.active.values())


def test_sample_enospc_marks_interrupted_and_reraises(cap, monkeypatch):
  report(cap)
  with monkeypatch.context() as fault:
    fault.setattr(C, 'open', no_space, raising=False)
    with pytest.raises(OSError):
      cap.sample({'t':110})
  assert cap.active[ID]['capture_interrupted']
  cap.finish(121)
  assert P.read_json(str(cap.events / ID / 'event.json'))['capture_interrupted']


@pytest.mark.parametrize('finalizing', [False, True])
def test_actual_directory_fsync_failure_retries_renamed_metadata(cap, monkeypatch, finalizing):
  if finalizing:
    report(cap)
  original = os.fsync
  def fail_directory(fd):
    directory = cap.events / ID
    if stat.S_ISDIR(os.fstat(fd).st_mode) and directory.exists() and os.fstat(fd).st_ino == directory.stat().st_ino:
      no_space()
    return original(fd)
  with monkeypatch.context() as fault:
    fault.setattr(os, 'fsync', fail_directory)
    with pytest.raises(OSError):
      cap.finish(121) if finalizing else report(cap)
  assert cap.active[ID]['state'] == 'capturing'
  if finalizing:
    cap.finish(126)
    assert ID not in cap.active
  else:
    report(cap)
    assert P.read_json(str(cap.events / ID / 'event.json'))['state'] == 'capturing'


def test_pre_roll_survives_long_outage_and_finish_waits_for_report_retry(cap, monkeypatch):
  with monkeypatch.context() as fault:
    fault.setattr(C, 'open', no_space, raising=False)
    with pytest.raises(OSError):
      report(cap, ['steering_bite'])
  cap.sample({'t':110})
  cap.ring.clear()
  cap.finish(160)
  assert cap.active[ID]['state'] == 'capturing'
  report(cap, ['steering_bite'])  # daemon uses original acceptance time, not real retry time
  cap.finish(160)
  assert ID not in cap.active
  assert json.loads((cap.events / ID / 'telemetry.jsonl').read_text())['t'] == 99
  event = P.read_json(str(cap.events / ID / 'event.json'))
  assert event['capture_interrupted'] and event['labels'] == ['steering_bite']


def test_status_failure_can_retry_saved_report(cap, monkeypatch):
  original = P.atomic_json
  def fail(path, *args, **kwargs):
    if path == P.STATUS:
      no_space()
    return original(path, *args, **kwargs)
  with monkeypatch.context() as fault:
    fault.setattr(P, 'atomic_json', fail)
    with pytest.raises(OSError):
      report(cap, ['unnecessary_slowdown'])
  report(cap, ['unnecessary_slowdown'])
  assert len(cap.rules['rules']) == 1
  assert P.read_json(P.STATUS)['saved']
