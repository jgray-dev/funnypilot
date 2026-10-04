import asyncio
import io
import tarfile
import threading
from pathlib import Path

import pytest
from aiohttp import web, ClientPayloadError
from aiohttp.test_utils import TestClient, TestServer
from openpilot.sunnypilot.navd import drive_download as dd, drive_index as di, nav_webserver as nw

ROUTE = 'abc123|2026-10-04--14-22-01'


@pytest.fixture
def recording(tmp_path, monkeypatch):
  monkeypatch.setattr(di, 'REALDATA_ROOT', str(tmp_path))
  directory = tmp_path / f'{ROUTE}--0'
  directory.mkdir()
  (directory/'qcamera.ts').write_bytes(b'video'*100)
  (directory/'qlog.zst').write_bytes(b'log'*100)
  return directory


def archive_files(data):
  with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    return {m.name: archive.extractfile(m).read() for m in archive.getmembers()}


def test_growing_file_uses_fixed_size_and_preserves_next_member(recording):
  stream = dd.tar_chunks(ROUTE, [0], ['qcamera.ts', 'qlog.zst'], di.segment_path)
  first = next(stream)  # opened file + fixed-size tar header
  with (recording/'qcamera.ts').open('ab') as file:
    file.write(b'new frames' * 1000)
  data = first + b''.join(stream)
  assert archive_files(data) == {f'{ROUTE}--0/qcamera.ts': b'video'*100, f'{ROUTE}--0/qlog.zst': b'log'*100}


def test_retention_unlink_after_open_does_not_lose_bytes(recording):
  stream = dd.tar_chunks(ROUTE, [0], ['qcamera.ts'], di.segment_path)
  first = next(stream)
  (recording/'qcamera.ts').unlink()
  assert archive_files(first+b''.join(stream))[f'{ROUTE}--0/qcamera.ts'] == b'video'*100


def test_truncation_is_an_error_not_silent_padding(recording):
  stream = dd.tar_chunks(ROUTE, [0], ['qcamera.ts'], di.segment_path)
  next(stream)
  (recording/'qcamera.ts').write_bytes(b'')
  with pytest.raises(OSError, match='truncated'):
    list(stream)


def test_reads_are_bounded_and_generator_close_releases_file(recording, monkeypatch):
  (recording/'qcamera.ts').write_bytes(b'x' * (dd.CHUNK_SIZE*3+15))
  real_open = open
  files = []
  class Tracked:
    def __init__(self, *args):
      self.file = real_open(*args)
      files.append(self.file)
    def fileno(self):
      return self.file.fileno()
    def read(self, size):
      assert 0 < size <= dd.CHUNK_SIZE
      return self.file.read(size)
    def __enter__(self):
      return self
    def __exit__(self, *args):
      self.file.close()
  monkeypatch.setattr(dd, 'open', Tracked, raising=False)
  stream = dd.tar_chunks(ROUTE, [0], ['qcamera.ts'], di.segment_path)
  next(stream)
  assert len(next(stream)) == dd.CHUNK_SIZE
  stream.close()
  assert files and all(f.closed for f in files)


@pytest.mark.asyncio
@pytest.mark.parametrize('what,names', [('video', {'qcamera.ts'}), ('data', {'qlog.zst'}), ('all', {'qcamera.ts', 'qlog.zst'})])
async def test_onroad_downloads_return_valid_archives(recording, monkeypatch, what, names):
  monkeypatch.setattr(nw, '_onroad', lambda: True)
  worker_threads = []
  original = dd.tar_chunks
  def tracked(*args):
    worker_threads.append(threading.get_ident())
    yield from original(*args)
  monkeypatch.setattr(dd, 'tar_chunks', tracked)
  app = web.Application(middlewares=[nw._never_5xx])
  app.router.add_get('/api/drives/{route}/download', nw.handle_drive_download)
  async with TestClient(TestServer(app)) as client:
    response = await client.get(f'/api/drives/{ROUTE}/download?what={what}')
    assert response.status == 200
    files = archive_files(await response.read())
  assert {Path(n).name for n in files} == names
  assert worker_threads and all(t != threading.get_ident() for t in worker_threads)


@pytest.mark.asyncio
async def test_disconnect_closes_generator_on_worker(recording, monkeypatch):
  opened, closed, release = threading.Event(), threading.Event(), threading.Event()
  def slow_chunks(*args):
    try:
      opened.set()
      yield b'x'*512
      assert release.wait(5)
      for _ in range(100):
        yield b'x'*dd.CHUNK_SIZE
    finally:
      closed.set()
  monkeypatch.setattr(dd, 'tar_chunks', slow_chunks)
  app = web.Application()
  app.router.add_get('/api/drives/{route}/download', nw.handle_drive_download)
  async with TestClient(TestServer(app)) as client:
    response = await client.get(f'/api/drives/{ROUTE}/download')
    assert await asyncio.to_thread(opened.wait, 2)
    response.close()
    release.set()
    assert await asyncio.to_thread(closed.wait, 3)


@pytest.mark.asyncio
async def test_midstream_failure_aborts_instead_of_successful_corrupt_archive(recording, monkeypatch):
  def broken(*args):
    yield b'x'*512
    raise OSError('truncated')
  monkeypatch.setattr(dd, 'tar_chunks', broken)
  app = web.Application()
  app.router.add_get('/api/drives/{route}/download', nw.handle_drive_download)
  async with TestClient(TestServer(app)) as client:
    response = await client.get(f'/api/drives/{ROUTE}/download')
    with pytest.raises(ClientPayloadError):
      await response.read()


@pytest.mark.asyncio
async def test_invalid_export_selector_and_missing_route_keep_errors(recording):
  app = web.Application()
  app.router.add_get('/api/drives/{route}/download', nw.handle_drive_download)
  async with TestClient(TestServer(app)) as client:
    response = await client.get(f'/api/drives/{ROUTE}/download?what=../../cloud.json')
    assert response.status == 400
    response = await client.get('/api/drives/abc123|2026-10-03--14-22-01/download')
    assert response.status == 404
