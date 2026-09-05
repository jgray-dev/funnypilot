"""Real temporary recordings through save APIs and production cleanup decisions.

Only uploader/native xattr imports are isolated on this x86 host. No actual
recordings, device paths, log parsing, or vehicle processes are involved.
"""
import ast
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace as NS

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.system.loggerd import drive_retention as retention
from openpilot.sunnypilot.navd import drive_index as drives
from openpilot.sunnypilot.navd import nav_webserver as server

ROOT = Path(__file__).resolve().parents[3]
OLD = '2026-08-01--10-00-00'
NEW = '2026-09-05--10-00-00'
NOW = 1_800_000_000


def segment(root, route=OLD, n=0, age=10):
  path = root / f'{route}--{n}'
  path.mkdir(exist_ok=True)
  (path / 'qcamera.ts').write_bytes(b'video')
  os.utime(path, (NOW - age * 86400, NOW - age * 86400))
  return path


@pytest.fixture
def cleaner(monkeypatch, tmp_path):
  # Run the real uploader directory-order helpers without loading its native IPC.
  src = ROOT / 'system/loggerd/uploader.py'
  tree = ast.parse(src.read_text())
  uploader = ModuleType('openpilot.system.loggerd.uploader')
  uploader.os = os
  uploader.cloudlog = NS(exception=lambda *a: None)
  helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and
             n.name in ('get_directory_sort', 'listdir_by_creation')]
  exec(compile(ast.Module(body=helpers, type_ignores=[]), str(src), 'exec'), uploader.__dict__)
  monkeypatch.setitem(sys.modules, uploader.__name__, uploader)
  attrs = ModuleType('openpilot.system.loggerd.xattr_cache')
  attrs.getxattr = lambda *a: None
  monkeypatch.setitem(sys.modules, attrs.__name__, attrs)
  spec = importlib.util.spec_from_file_location('_retention_deleter', ROOT / 'system/loggerd/deleter.py')
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module, 'Paths', NS(log_root=lambda: str(tmp_path), log_root_external=lambda: str(tmp_path / 'external')))
  monkeypatch.setattr(module, 'get_available_bytes', lambda **kw: 20 * 1024**3)
  monkeypatch.setattr(module, 'get_available_percent', lambda **kw: 50)
  monkeypatch.setattr(retention.time, 'time', lambda: NOW)
  monkeypatch.setattr(drives, 'REALDATA_ROOT', str(tmp_path))
  return module


def test_saved_whole_drive_survives_age_pressure_and_new_segments(cleaner, tmp_path, monkeypatch):
  old = segment(tmp_path)
  segment(tmp_path, NEW, age=0)
  assert drives.set_route_saved(OLD, True, str(tmp_path))
  later = segment(tmp_path, OLD, n=1)
  assert not cleaner.cleanup_once()  # saved, despite being older than retention
  monkeypatch.setattr(cleaner, 'get_available_bytes', lambda **kw: 0)
  assert cleaner.cleanup_once()  # space pressure can evict NEW, never OLD
  assert not cleaner.cleanup_once()
  assert old.exists() and later.exists()
  assert drives.scan_routes(str(tmp_path))[0]['saved']
  assert drives.storage_stats(str(tmp_path))['saved_bytes'] == 10
  # Read fresh from disk, as after a restart/flash, not from any process cache.
  assert retention.read_saved(str(tmp_path)) == {OLD}


def test_unsave_restores_automatic_expiry(cleaner, tmp_path):
  old = segment(tmp_path)
  new = segment(tmp_path, NEW, age=0)
  drives.set_route_saved(OLD, True, str(tmp_path))
  drives.set_route_saved(OLD, False, str(tmp_path))
  assert cleaner.cleanup_once()
  assert not old.exists() and new.exists()


def test_normal_space_expires_complete_old_routes_but_keeps_latest(cleaner, tmp_path):
  old = [segment(tmp_path, n=n) for n in range(3)]
  latest = segment(tmp_path, NEW, age=9)
  for _ in old:
    assert cleaner.cleanup_once()
  assert not cleaner.cleanup_once()
  assert all(not p.exists() for p in old) and latest.exists()


def test_recent_segment_or_recording_lock_keeps_whole_route(cleaner, tmp_path):
  first = segment(tmp_path)
  last = segment(tmp_path, n=1, age=0)
  segment(tmp_path, NEW, age=0)
  assert not cleaner.cleanup_once()
  os.utime(last, (NOW - 10 * 86400, NOW - 10 * 86400))
  (last / 'rlog.lock').touch()
  os.utime(last, (NOW - 10 * 86400, NOW - 10 * 86400))
  assert not cleaner.cleanup_once()
  with pytest.raises(retention.ActiveDriveError):
    drives.delete_route(OLD, str(tmp_path))
  assert first.exists() and last.exists()


def test_external_cleanup_also_honors_saved_routes(cleaner, tmp_path, monkeypatch):
  segment(tmp_path)
  drives.set_route_saved(OLD, True, str(tmp_path))
  external = tmp_path / 'external'
  external.mkdir()
  saved = segment(external)
  expendable = segment(external, NEW, age=0)
  monkeypatch.setattr(cleaner.Path, 'is_mount', lambda self: str(self) == str(external))
  monkeypatch.setattr(cleaner, 'get_available_bytes', lambda **kw: 0 if kw.get('path_type') == 'external' else 20 * 1024**3)
  assert cleaner.cleanup_once()
  assert saved.exists() and not expendable.exists()
  assert not cleaner.cleanup_once()


@pytest.mark.parametrize('contents', ['{', '{"version":1,"routes":["../escape"]}', '{"version":2,"routes":[]}'])
def test_bad_metadata_pauses_cleanup(cleaner, tmp_path, contents):
  old = segment(tmp_path)
  segment(tmp_path, NEW, age=0)
  (tmp_path / retention._MANIFEST).write_text(contents)
  with pytest.raises(ValueError):
    cleaner.cleanup_once()
  assert old.exists()


def test_saved_metadata_cannot_be_a_symlink(cleaner, tmp_path):
  old = segment(tmp_path)
  (tmp_path / 'elsewhere').write_text('{"version":1,"routes":[]}')
  (tmp_path / retention._MANIFEST).symlink_to(tmp_path / 'elsewhere')
  with pytest.raises(OSError):
    cleaner.cleanup_once()
  assert old.exists()


def test_save_and_cleanup_share_a_process_lock(cleaner, tmp_path):
  old = segment(tmp_path)
  segment(tmp_path, NEW, age=0)
  entered, finished = threading.Event(), threading.Event()
  failures = []

  def clean():
    entered.set()
    try:
      cleaner.cleanup_once()
    except Exception as e:
      failures.append(e)
    finally:
      finished.set()

  with retention.locked_saved(str(tmp_path)) as saved:
    worker = threading.Thread(target=clean)
    worker.start()
    assert entered.wait(1)
    assert not finished.wait(.05)
    saved.add(OLD)
    retention.write_saved(str(tmp_path), saved)
  worker.join(2)
  assert finished.is_set() and not failures and old.exists()


def test_no_candidates_waits_a_full_minute(cleaner, tmp_path, monkeypatch):
  segment(tmp_path)
  drives.set_route_saved(OLD, True, str(tmp_path))
  monkeypatch.setattr(cleaner, 'get_available_bytes', lambda **kw: 0)
  waits = []
  cleaner.deleter_thread(NS(is_set=lambda: bool(waits), wait=waits.append))
  assert waits == [60.0]


def test_save_api_persists_and_protects_delete(cleaner, tmp_path):
  old = segment(tmp_path)

  async def exercise():
    app = web.Application()
    app.router.add_get('/api/drives', server.handle_drives)
    app.router.add_get('/api/drives/{route}', server.handle_drive_detail)
    app.router.add_put('/api/drives/{route}/saved', server.handle_drive_save)
    app.router.add_delete('/api/drives/{route}', server.handle_drive_delete)
    async with TestClient(TestServer(app)) as client:
      url = f'/api/drives/{OLD}'
      result = await client.put(url + '/saved', json={'saved': True})
      assert result.status == 200 and (await result.json())['saved']
      detail = await client.get(url)
      assert (await detail.json())['saved']
      listing = await client.get('/api/drives')
      assert (await listing.json())['drives'][0]['saved']
      assert (await client.delete(url)).status == 409
      assert old.exists()
      assert (await client.put(url + '/saved', json={'saved': 'false'})).status == 400
      assert (await client.put(url + '/saved', data='{', headers={'Content-Type': 'application/json'})).status == 400
      assert (await client.put('/api/drives/absent/saved', json={'saved': True})).status == 404
      assert (await client.put(url + '/saved', json={'saved': False})).status == 200
      assert (await client.delete(url)).status == 200
      assert not old.exists()

  asyncio.run(exercise())
