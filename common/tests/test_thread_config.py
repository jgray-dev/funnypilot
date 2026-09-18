import os
import threading

import pytest

from openpilot.common.thread_config import configure_background_thread, HOUSEKEEPING_CPUS


def test_priority_failure_does_not_skip_affinity(monkeypatch):
  calls = []

  def denied(*args):
    raise PermissionError('fixture')

  monkeypatch.setattr(os, 'sched_setscheduler', denied)
  monkeypatch.setattr(os, 'sched_setaffinity', lambda tid, cores: calls.append((tid, tuple(cores))))
  configure_background_thread()
  assert calls == [(0, (0, 1, 2, 3))]


def test_demotes_before_moving_thread(monkeypatch):
  calls = []
  monkeypatch.setattr(os, 'sched_setscheduler', lambda tid, policy, param: calls.append((tid, policy, param.sched_priority)))
  monkeypatch.setattr(os, 'sched_setaffinity', lambda tid, cores: calls.append((tid, tuple(cores))))
  configure_background_thread()
  assert calls == [(0, os.SCHED_OTHER, 0), (0, HOUSEKEEPING_CPUS)]


def test_native_worker_affinity_is_widened_without_changing_parent():
  if not hasattr(os, 'sched_getaffinity'):
    pytest.skip('Linux affinity required')
  parent = os.sched_getaffinity(0)
  if not set(HOUSEKEEPING_CPUS) <= parent:
    pytest.skip('Requires C3X housekeeping CPUs in the test process cpuset')
  results = []

  def worker():
    os.sched_setaffinity(0, {3})
    configure_background_thread()
    results.append((os.sched_getaffinity(0), os.sched_getscheduler(0)))

  thread = threading.Thread(target=worker)
  thread.start()
  thread.join(2)
  assert not thread.is_alive()
  assert results == [(set(HOUSEKEEPING_CPUS), os.SCHED_OTHER)]
  assert os.sched_getaffinity(0) == parent
