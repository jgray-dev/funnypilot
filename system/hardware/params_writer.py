"""Coalesced hardware status persistence, independent of deviceState publication.

Params put AND remove take the global disk lock. Only this worker may do either
for status/telemetry. A fixed key set bounds memory even if storage stops forever.
This is not a queue for commands: intermediate status values may be superseded.
"""
import copy
import threading
import time
from collections import OrderedDict


HARDWARE_STATUS_KEYS = frozenset({
  'IsEngaged', 'GithubRunnerSufficientVoltage', 'NetworkMetered',
  'UptimeOffroad', 'UptimeOnroad', 'LastOffroadStatusPacket', 'CarBatteryCapacity',
  'Offroad_TiciSupport', 'Offroad_TemperatureTooHigh',
})


class HardwareParamsWriter:
  def __init__(self, params, report=None):
    self.params = params
    self.report = report
    self._condition = threading.Condition()
    self._desired = {}
    self._pending = OrderedDict()
    self._stopped = False
    self._thread = threading.Thread(target=self._run, name='hardware-params', daemon=True)
    self._thread.start()

  def put(self, key, value):
    if key not in HARDWARE_STATUS_KEYS:
      raise ValueError(f'Not a hardware status key: {key}')
    value = copy.deepcopy(value)
    with self._condition:
      if key in self._desired and self._desired[key] == value:
        return
      self._desired[key] = value
      self._pending[key] = value
      self._condition.notify()

  def put_bool(self, key, value):
    self.put(key, bool(value))

  def remove(self, key):
    self.put(key, None)

  def close(self, timeout=1.):
    with self._condition:
      self._stopped = True
      self._condition.notify()
    self._thread.join(timeout)

  def _run(self):
    failing = False
    while True:
      with self._condition:
        self._condition.wait_for(lambda: self._pending or self._stopped)
        if self._stopped:
          return
        key, value = self._pending.popitem(last=False)
      started = time.monotonic()
      ok = False
      try:
        # Avoid even taking the global Params lock when nothing changed.
        if self.params.get(key) != value:
          if value is None:
            self.params.remove(key)
          else:
            self.params.put(key, value)
        # Native Params discards the write return code; verify persistence.
        ok = self.params.get(key) == value
      except Exception:
        pass
      duration = time.monotonic() - started
      if self.report is not None and ((not ok and not failing) or duration > 0.5):
        try:
          self.report('hardware_status_persistence', key=key, success=ok, duration_s=round(duration, 3))
        except Exception:
          pass
      failing = not ok
      with self._condition:
        if not ok and key not in self._pending:
          self._pending[key] = value
        if not ok:
          self._condition.wait(timeout=0.5)
