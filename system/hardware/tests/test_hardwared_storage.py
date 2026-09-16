"""Native Params fault injection in an isolated directory, never live IPC/Params.

Run on the Comma (or a native openpilot build). Hardware and message transport
are fixtures; the production hardware_thread and compiled Params are real.
"""
import fcntl
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from cereal import log
from openpilot.common.params import Params
from openpilot.system.hardware import hardwared, power_monitoring
from openpilot.selfdrive.selfdrived import alertmanager


@pytest.mark.parametrize('temperature, offroad, expected_started', [(50., False, True), (120., False, False), (50., True, False)])
def test_status_publishes_while_native_params_lock_is_held(tmp_path, monkeypatch, temperature, offroad, expected_started):
  root = tmp_path / 'params'
  params = Params(str(root))
  for key, value in {
    'HasAcceptedTerms': hardwared.terms_version,
    'HasAcceptedTermsSP': hardwared.terms_version_sp,
    'CompletedTrainingVersion': hardwared.training_version,
    'UptimeOnroad': 0., 'UptimeOffroad': 0.,
    'OffroadMode': offroad,
  }.items():
    params.put(key, value)
  monkeypatch.setattr(hardwared, 'Params', lambda: params)
  monkeypatch.setattr(power_monitoring, 'Params', lambda: params)
  monkeypatch.setattr(alertmanager, 'Params', lambda: params)

  temperatures = {'cpuTempC': [temperature] * 8, 'gpuTempC': [temperature], 'memoryTempC': temperature, 'pmicTempC': [temperature]}
  hardware = SimpleNamespace(
    initialize_hardware=lambda: None,
    get_thermal_config=lambda: SimpleNamespace(get_msg=lambda: temperatures),
    get_device_type=lambda: 'tizi', get_gpu_usage_percent=lambda: 0.,
    get_screen_brightness=lambda: 50., booted=lambda: True,
    set_power_save=lambda value: None,
    get_current_power_draw=lambda: 5., get_som_power_draw=lambda: 2.,
  )
  monkeypatch.setattr(hardwared, 'HARDWARE', hardware)
  monkeypatch.setattr(power_monitoring, 'HARDWARE', hardware)
  monkeypatch.setattr(hardwared, 'get_available_percent', lambda **kwargs: 50.)
  monkeypatch.setattr(hardwared, 'get_build_metadata', lambda: SimpleNamespace(channel_type='tizi', channel='test'))
  monkeypatch.setattr(hardwared, 'cloudlog', SimpleNamespace(event=lambda *a, **kw: None, warning=lambda *a: None))
  monkeypatch.setattr(hardwared, 'statlog', SimpleNamespace(sample=lambda *a: None, gauge=lambda *a: None))

  class Inputs:
    frame = 0
    updated = {'pandaStates': True, 'selfdriveState': False}
    alive = {'gpsLocationExternal': False}
    recv_time = {}

    def __init__(self, *args, **kwargs):
      self.data = {
        'pandaStates': [log.PandaState.new_message(pandaType='tres', ignitionLine=True, harnessStatus='normal')],
        'peripheralState': log.PeripheralState.new_message(pandaType='tres', voltage=12000),
        'selfdriveState': log.SelfdriveState.new_message(enabled=True),
      }

    def update(self, timeout):
      time.sleep(0.025)
      self.frame += round(hardwared.SERVICE_LIST['pandaStates'].frequency * hardwared.DT_HW)
      self.recv_time['pandaStates'] = time.monotonic()

    def __getitem__(self, key):
      return self.data[key]

  end, published = threading.Event(), threading.Event()
  messages, errors = [], []

  class Output:
    def __init__(self, *args):
      pass

    def send(self, topic, msg):
      assert topic == 'deviceState'
      messages.append((time.monotonic(), msg.deviceState.to_dict()))
      if len(messages) >= 20:
        published.set()

  monkeypatch.setattr(hardwared.messaging, 'SubMaster', Inputs)
  monkeypatch.setattr(hardwared.messaging, 'PubMaster', Output)

  def run():
    try:
      hardwared.hardware_thread(end, queue.Queue())
    except Exception as exc:
      errors.append(exc)

  # The lock is private to this test. Holding it reproduces a slow concurrent
  # Params fsync without touching the running manager or any vehicle process.
  with open(root / '.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
      assert published.wait(3), f'Publisher stalled behind the Params lock: {errors}'
      assert not errors
      # The initial one-second onroad-cycle hold is part of the production gate.
      assert all(row['started'] == expected_started for _, row in messages[-10:])
      assert all(row['thermalStatus'] == ('danger' if temperature == 120 else 'green') for _, row in messages[-10:])
      assert params.get('IsEngaged') is None, 'writer should still be blocked'
    finally:
      fcntl.flock(lock, fcntl.LOCK_UN)
      end.set()
      thread.join(3)
    assert not thread.is_alive()
    assert not errors
