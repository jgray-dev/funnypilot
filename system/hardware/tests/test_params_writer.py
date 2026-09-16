import ast
import threading
import time
from pathlib import Path

import pytest

from openpilot.system.hardware.params_writer import HARDWARE_STATUS_KEYS, HardwareParamsWriter


def eventually(check):
  deadline = time.monotonic() + 3
  while not check() and time.monotonic() < deadline:
    time.sleep(0.01)
  assert check()


class MemoryParams:
  def __init__(self):
    self.data = {}
    self.calls = []

  def get(self, key):
    return self.data.get(key)

  def put(self, key, value):
    self.calls.append((key, value))
    self.data[key] = value

  def remove(self, key):
    self.calls.append((key, None))
    self.data.pop(key, None)


def test_blocked_storage_is_bounded_and_latest_status_wins():
  params = MemoryParams()
  entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
  original_put = params.put

  def blocked_put(key, value):
    entered.set()
    assert release.wait(5)
    original_put(key, value)

  params.put = blocked_put
  writer = HardwareParamsWriter(params)
  writer.put('UptimeOnroad', 0.)
  assert entered.wait(1)

  def producer():
    for i in range(1000):
      writer.put('UptimeOnroad', float(i))
      writer.put_bool('IsEngaged', i % 2)
      writer.remove('Offroad_TiciSupport')
    submitted.set()

  producer_thread = threading.Thread(target=producer)
  producer_thread.start()
  try:
    assert submitted.wait(1), 'hardware publication would be waiting for storage'
    assert len(writer._pending) == 3
    assert len(writer._desired) <= len(HARDWARE_STATUS_KEYS)
    release.set()
    eventually(lambda: params.get('UptimeOnroad') == 999. and params.get('IsEngaged') is True)
    assert [v for k, v in params.calls if k == 'UptimeOnroad'] == [0., 999.]
  finally:
    release.set()
    producer_thread.join(1)
    writer.close()


def test_unchanged_status_and_absent_alert_never_mutate_disk():
  params = MemoryParams()
  params.data['NetworkMetered'] = False
  writer = HardwareParamsWriter(params)
  try:
    for _ in range(100):
      writer.put_bool('NetworkMetered', False)
      writer.remove('Offroad_TiciSupport')
    # A later FIFO item confirms the earlier requests were processed.
    writer.put('UptimeOnroad', 10.)
    eventually(lambda: params.get('UptimeOnroad') == 10.)
    assert params.calls == [('UptimeOnroad', 10.)]
  finally:
    writer.close()


@pytest.mark.parametrize('failure', ['exception', 'unacknowledged'])
def test_failed_writes_retry_without_a_new_status_change(failure):
  params = MemoryParams()
  attempts = []
  original_put = params.put

  def fail_once(key, value):
    attempts.append(value)
    if len(attempts) == 1:
      if failure == 'exception':
        raise OSError('disk error')
      return
    original_put(key, value)

  params.put = fail_once
  writer = HardwareParamsWriter(params)
  try:
    writer.put_bool('IsEngaged', True)
    eventually(lambda: params.get('IsEngaged') is True)
    assert attempts == [True, True]
  finally:
    writer.close()


def test_superseded_failed_write_cannot_restore_old_state():
  params = MemoryParams()
  entered, release = threading.Event(), threading.Event()
  original_put = params.put

  def fail_old(key, value):
    if value is True:
      entered.set()
      assert release.wait(3)
      raise OSError('failed old state')
    original_put(key, value)

  params.put = fail_old
  writer = HardwareParamsWriter(params)
  try:
    writer.put_bool('IsEngaged', True)
    assert entered.wait(1)
    writer.put_bool('IsEngaged', False)
    release.set()
    eventually(lambda: params.get('IsEngaged') is False)
    assert params.calls == [('IsEngaged', False)]
  finally:
    release.set()
    writer.close()


def test_alert_payload_is_owned_and_commands_are_rejected():
  params = MemoryParams()
  writer = HardwareParamsWriter(params)
  try:
    alert = {'extra': 'hot'}
    writer.put('Offroad_TemperatureTooHigh', alert)
    alert['extra'] = 'changed'
    eventually(lambda: params.get('Offroad_TemperatureTooHigh') == {'extra': 'hot'})
    writer.remove('Offroad_TemperatureTooHigh')
    eventually(lambda: params.get('Offroad_TemperatureTooHigh') is None)
    with pytest.raises(ValueError):
      writer.put_bool('DoShutdown', True)
  finally:
    writer.close()


def test_publisher_never_writes_status_through_native_params():
  tree = ast.parse((Path(__file__).parents[1] / 'hardwared.py').read_text())
  hardware = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'hardware_thread')
  for call in (n for n in ast.walk(hardware) if isinstance(n, ast.Call)):
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == 'params':
      if call.func.attr in ('put', 'put_bool', 'put_nonblocking', 'put_bool_nonblocking', 'remove'):
        # Explicit transition/shutdown commands retain their synchronous ordering.
        assert call.args[0].value in ('OnroadCycleRequested', 'DoShutdown')
    if isinstance(call.func, ast.Name) and call.func.id == 'set_offroad_alert':
      assert any(k.arg == 'params' and isinstance(k.value, ast.Name) and k.value.id == 'status_writer' for k in call.keywords)
