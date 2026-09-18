"""Production map pipeline with native private Params and fixture transport.

Run on a native build/device. No live IPC subscriptions or vehicle Params.
"""
import fcntl
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from cereal import log
from openpilot.common.params import Params
from openpilot.sunnypilot.mapd import mapd_manager
from openpilot.sunnypilot.mapd.live_map_data import base_map_data, osm_map_data


def test_selfdrived_entrypoint_reserves_core_four_for_card_and_controls(monkeypatch):
  from openpilot.selfdrive.selfdrived import selfdrived
  calls = []
  monkeypatch.setattr(selfdrived, 'config_realtime_process', lambda cores, priority: calls.append((cores, priority)))
  monkeypatch.setattr(selfdrived, 'SelfdriveD', lambda: SimpleNamespace(run=lambda: calls.append('run')))
  selfdrived.main()
  assert calls == [([0, 1, 2, 3], selfdrived.Priority.CTRL_HIGH), 'run']


def test_maintenance_only_persists_alert_transitions(tmp_path, monkeypatch):
  disk = Params(str(tmp_path / 'disk'))
  mem = Params(str(tmp_path / 'mem'))
  disk.put('MapdVersion', mapd_manager.VERSION)
  disk.put_bool('OsmLocal', False)
  monkeypatch.setattr(mapd_manager, 'Params', lambda path=None: disk if path is None else mem)
  monkeypatch.setattr(mapd_manager.Paths, 'mapd_root', lambda: str(tmp_path / 'maps'))
  monkeypatch.setattr(mapd_manager, 'configure_background_thread', lambda: None)
  monkeypatch.setattr(mapd_manager, 'get_files_for_cleanup', lambda: ['old map'])
  changes = []
  original = mapd_manager.set_offroad_alert

  def alert(name, show, extra, *, params):
    changes.append(show)
    original(name, show, extra, params=params)

  monkeypatch.setattr(mapd_manager, 'set_offroad_alert', alert)

  class Stop:
    ticks = 0

    def is_set(self):
      return self.ticks == 4

    def wait(self, seconds):
      self.ticks += 1
      disk.put_bool('OsmLocal', self.ticks in (1, 2))

  mapd_manager.maintenance_thread(Stop())
  assert changes == [True, False]
  assert disk.get('Offroad_OSMUpdateRequired') is None


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
  mem = Params(str(tmp_path / 'mem'))
  mem.put('MapSpeedLimit', 15.6464)
  mem.put('NextMapSpeedLimit', {'speedlimit': 20.0, 'latitude': 1.001, 'longitude': 2.001})
  location = log.LiveLocationKalman.new_message()
  location.status = 'valid'
  location.gpsOK = True
  location.positionGeodetic.valid = True
  location.positionGeodetic.value = [1.0, 2.0, 0.0]
  location.calibratedOrientationNED.valid = True
  location.calibratedOrientationNED.value = [0.0, 0.0, 0.5]

  class Inputs:
    def __init__(self, *args):
      self.recv_time = {'liveLocationKalman': time.monotonic()}
      self.valid = {'liveLocationKalman': True}
      self.refresh = True

    def update(self, timeout):
      if self.refresh:
        self.recv_time['liveLocationKalman'] = time.monotonic()

    def __getitem__(self, key):
      assert key == 'liveLocationKalman'
      return location

  messages = []
  published = threading.Event()

  class Output:
    def __init__(self, *args):
      pass

    def send(self, topic, message):
      assert topic == 'liveMapDataSP'
      messages.append((time.monotonic(), message.valid, message.liveMapDataSP.to_dict()))
      if len(messages) >= 3:
        published.set()

  monkeypatch.setattr(base_map_data.messaging, 'SubMaster', Inputs)
  monkeypatch.setattr(base_map_data.messaging, 'PubMaster', Output)
  monkeypatch.setattr(osm_map_data, 'Params', lambda *args: mem)
  return SimpleNamespace(mem=mem, location=location, messages=messages, published=published)


def test_publishes_while_native_disk_params_lock_is_held(tmp_path, monkeypatch, pipeline):
  disk = Params(str(tmp_path / 'disk'))
  monkeypatch.setattr(mapd_manager, 'Params', lambda path=None: disk if path is None else pipeline.mem)
  monkeypatch.setattr(mapd_manager.Paths, 'mapd_root', lambda: str(tmp_path / 'maps'))
  monkeypatch.setattr(mapd_manager, 'config_realtime_process', lambda *args: None)
  monkeypatch.setattr(mapd_manager, 'get_files_for_cleanup', list)
  stop = threading.Event()
  errors = []

  def run():
    try:
      mapd_manager.main_thread(stop)
    except Exception as exc:
      errors.append(exc)

  with open(tmp_path / 'disk' / '.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
      assert pipeline.published.wait(3.5), f'map publisher stalled: {errors}'
      assert not errors
      assert disk.get('MapdVersion') is None, 'maintenance should still be blocked on the private lock'
      assert all(valid for _, valid, _ in pipeline.messages)
      assert json.loads(pipeline.mem.get('LastGPSPosition'))['latitude'] == 1.0
      gaps = [b[0] - a[0] for a, b in zip(pipeline.messages, pipeline.messages[1:], strict=False)]
      assert max(gaps) < 1.5
    finally:
      stop.set()
      fcntl.flock(lock, fcntl.LOCK_UN)
      thread.join(3)
  assert not thread.is_alive()


@pytest.mark.parametrize('failure', ['stale', 'unseen', 'transport', 'gps', 'position', 'orientation'])
def test_old_or_invalid_location_does_not_refresh_position_or_validate_map(pipeline, failure):
  producer = osm_map_data.OsmMapData()
  producer.tick()
  assert pipeline.messages[-1][1] is True
  path = pipeline.mem.get_param_path('LastGPSPosition')
  stamp = os.stat(path).st_mtime_ns
  producer.sm.refresh = False
  if failure == 'stale':
    producer.sm.recv_time['liveLocationKalman'] = time.monotonic() - 3
  elif failure == 'unseen':
    producer.sm.recv_time['liveLocationKalman'] = 0
  elif failure == 'transport':
    producer.sm.valid['liveLocationKalman'] = False
  elif failure == 'gps':
    pipeline.location.gpsOK = False
  elif failure == 'position':
    pipeline.location.positionGeodetic.valid = False
  else:
    pipeline.location.calibratedOrientationNED.valid = False
  producer.tick()
  assert os.stat(path).st_mtime_ns == stamp
  _, valid, data = pipeline.messages[-1]
  assert not valid and not data['speedLimitValid'] and not data['speedLimitAheadValid']

  producer.sm.refresh = True
  producer.sm.valid['liveLocationKalman'] = True
  pipeline.location.gpsOK = True
  pipeline.location.positionGeodetic.valid = True
  pipeline.location.calibratedOrientationNED.valid = True
  pipeline.location.positionGeodetic.value = [1.1, 2.1, 0.0]
  producer.tick()
  assert pipeline.messages[-1][1] is True
  assert json.loads(pipeline.mem.get('LastGPSPosition'))['latitude'] == pytest.approx(1.1)


def test_resumes_without_catchup_burst(monkeypatch):
  clock = [100.0]
  waits = []
  ticks = []

  class Stop:
    def is_set(self):
      return len(ticks) >= 4

    def set(self):
      pass

    def wait(self, duration):
      waits.append(duration)
      clock[0] += duration

  def tick():
    ticks.append(clock[0])
    if len(ticks) == 1:
      clock[0] += 180

  monkeypatch.setattr(mapd_manager, 'config_realtime_process', lambda *args: None)
  monkeypatch.setattr(mapd_manager, 'OsmMapData', lambda: SimpleNamespace(tick=tick))
  monkeypatch.setattr(mapd_manager, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
  monkeypatch.setattr(mapd_manager, 'threading', SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: None, join=lambda **kw: None)))
  mapd_manager.main_thread(Stop())
  assert ticks == [100., 280., 281., 282.]
  assert waits == [0., 1., 1., 1.]
