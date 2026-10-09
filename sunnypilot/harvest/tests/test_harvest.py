import hashlib
import os
import time

import pytest

from openpilot.sunnypilot.harvest import harvestd, retention as RT, segments as S, uploader as U
from openpilot.system.loggerd import drive_retention as R

OLD = time.time() - 3600  # noqa: TID251 - filesystem mtime fixtures


def make_segment(root, name, files=None, age=OLD, lock=False):
  path = root / name
  path.mkdir()
  for fname, data in (files or {'rlog.zst': b'r' * 100, 'fcamera.hevc': b'f' * 100}).items():
    (path / fname).write_bytes(data)
  if lock:
    (path / 'rlog.lock').write_bytes(b'')
  for p in [*path.iterdir(), path]:
    os.utime(p, (age, age))
  return str(path)


@pytest.fixture
def root(tmp_path):
  r = tmp_path / 'realdata'
  r.mkdir()
  probe = r / 'probe'
  probe.mkdir()
  try:
    os.setxattr(probe, S.MARK, b'1')
  except OSError:
    pytest.skip('filesystem has no user xattrs')
  probe.rmdir()
  return r


class FakeCloud:
  """Emulates the Worker's contract closely enough to prove the protocol."""
  def __init__(self):
    self.manifest, self.parts, self.complete, self.calls = None, {}, False, []

  def request(self, method, url, json=None, data=None, **kw):
    self.calls.append((method, url))
    if method == 'PUT' and '/files/' not in url:
      self.manifest = json
    elif method == 'PUT':
      name, index = url.split('/files/')[1].rsplit('/', 1)
      self.parts[(name, int(index))] = data
    else:
      for a in self.manifest['artifacts']:
        for i, p in enumerate(a['parts']):
          got = self.parts.get((a['name'], i))
          assert got is not None and hashlib.sha256(got).hexdigest() == p['sha256'] and len(got) == p['size']
      self.complete = True
    return type('R', (), {'status_code': 200, 'raise_for_status': lambda s: None, 'json': lambda s: {'ok': True}})()

  def __enter__(self):
    return self

  def __exit__(self, *a):
    return False

  headers = {}


def patch_cloud(monkeypatch):
  cloud = FakeCloud()
  monkeypatch.setattr(U.requests, 'Session', lambda: cloud)
  return cloud


CONFIG = {'endpoint': 'https://example.invalid', 'token': 'a' * 64}


def test_pending_skips_locked_fresh_marked_and_empty(root):
  ok = make_segment(root, '0000001--abc--0')
  make_segment(root, '0000001--abc--1', lock=True)
  make_segment(root, '0000001--abc--2', age=time.time())  # noqa: TID251 - still being written
  marked = make_segment(root, '0000001--abc--3')
  S.mark_harvested(marked)
  make_segment(root, '0000001--abc--4', files={'dcamera.hevc': b'cabin'})
  assert [p for _, _, p in S.pending_segments(str(root))] == [ok]


def test_cabin_camera_and_audio_are_never_listed(root):
  path = make_segment(root, '0000001--abc--0', files={'dcamera.hevc': b'x', 'audio.wav': b'y', 'rlog.zst': b'z'})
  assert S.segment_files(path) == ['rlog.zst']


def test_upload_streams_parts_verifies_and_marks(root, monkeypatch):
  monkeypatch.setattr(U, 'PART', 64)
  path = make_segment(root, '0000001--abc--0', files={'rlog.zst': os.urandom(150), 'fcamera.hevc': os.urandom(64)})
  cloud = patch_cloud(monkeypatch)
  assert U.upload_segment('0000001--abc', 0, path, CONFIG, {'commit': 'a' * 40}) is True
  assert cloud.complete and S.is_harvested(path)
  rlog = [a for a in cloud.manifest['artifacts'] if a['name'] == 'rlog.zst'][0]
  assert [p['size'] for p in rlog['parts']] == [64, 64, 22]
  assert b''.join(cloud.parts[('rlog.zst', i)] for i in range(3)) == (root / '0000001--abc--0' / 'rlog.zst').read_bytes()


def test_changed_file_is_never_marked(root, monkeypatch):
  path = make_segment(root, '0000001--abc--0')
  cloud = patch_cloud(monkeypatch)
  real = cloud.request
  def tamper(method, url, **kw):
    if method == 'PUT' and '/files/' not in url:  # after hashing, before streaming
      (root / '0000001--abc--0' / 'rlog.zst').write_bytes(b'changed' * 20)
    return real(method, url, **kw)
  cloud.request = tamper
  with pytest.raises(RuntimeError):
    U.upload_segment('0000001--abc', 0, path, CONFIG, {})
  assert not S.is_harvested(path) and not cloud.complete


def test_gate_closing_midway_stops_before_complete(root, monkeypatch):
  path = make_segment(root, '0000001--abc--0')
  cloud = patch_cloud(monkeypatch)
  state = {'n': 0}
  def allowed():
    state['n'] += 1
    return state['n'] < 3
  with pytest.raises(RuntimeError):
    U.upload_segment('0000001--abc', 0, path, CONFIG, {}, allowed=allowed)
  assert not cloud.complete and not S.is_harvested(path)


def test_route_with_pipe_is_url_encoded(root, monkeypatch):
  path = make_segment(root, 'a|b--0')
  cloud = patch_cloud(monkeypatch)
  U.upload_segment('a|b', 0, path, CONFIG, {})
  assert all('a%7Cb' in url for _, url in cloud.calls)


class Gate:
  def __init__(self, allowed=True):
    self.allowed = allowed

  def __call__(self):
    return self.allowed


def make_harvester(root, tmp_path, gate, upload, clock):
  cfg = tmp_path / 'cloud.json'
  cfg.write_text('{"endpoint":"https://example.invalid","token":"%s"}' % ('a' * 64))
  return harvestd.Harvester(str(root), str(cfg), gate, {}, clock=clock, upload=upload, status_path=str(tmp_path / 'status.json'))


def test_unlinked_or_offline_does_nothing(root, tmp_path):
  make_segment(root, '0000001--abc--0')
  called = []
  h = make_harvester(root, tmp_path, Gate(False), lambda *a, **k: called.append(1), lambda: 0.0)
  h.step()
  h.config_path = str(tmp_path / 'missing.json')
  h.gate = Gate(True)
  h.step()
  assert not called


def test_failure_backs_off_and_poison_segment_is_skipped(root, tmp_path):
  make_segment(root, '0000001--abc--0')
  now = [0.0]
  def boom(*a, **k):
    raise OSError('net')
  h = make_harvester(root, tmp_path, Gate(), boom, lambda: now[0])
  for _ in range(harvestd.MAX_FAILURES):
    h.step()
    now[0] += harvestd.RETRY_S + 1
  assert h.error == 'OSError'
  later = []
  h.upload = lambda *a, **k: later.append(1) or True
  h.step()
  assert not later  # skipped for SKIP_S, so other segments are not starved
  now[0] += harvestd.SKIP_S
  h.step()
  assert later


def test_expiry_waits_for_the_cloud_copy_only_inside_the_grace_period(root):
  now = time.time()  # noqa: TID251
  recent = make_segment(root, '0000001--abc--0', age=now - 10 * 86400)
  done = make_segment(root, '0000002--def--0', age=now - 10 * 86400)
  ancient = make_segment(root, '0000003--ghi--0', age=now - 40 * 86400)
  make_segment(root, '0000004--jkl--0')
  S.mark_harvested(done)
  dirs = sorted(os.listdir(root))
  expired = R.expired_routes(str(root), dirs)
  assert expired == {'0000001--abc', '0000002--def', '0000003--ghi'}
  held = RT.awaiting_harvest(str(root), dirs, expired)
  assert held == {'0000001--abc'} and os.path.isdir(recent) and os.path.isdir(ancient)


def test_pressure_order_spends_harvested_segments_first(root):
  make_segment(root, '0000001--abc--0')
  b = make_segment(root, '0000002--def--0')
  S.mark_harvested(b)
  names = sorted(os.listdir(root), key=lambda d: RT.unharvested_last(str(root), d))
  assert names == ['0000002--def--0', '0000001--abc--0']


def test_unlinked_device_is_not_harvesting(tmp_path):
  assert not RT.linked(str(tmp_path / 'cloud.json'))
  (tmp_path / 'cloud.json').write_text('{}')
  assert RT.linked(str(tmp_path / 'cloud.json'))
