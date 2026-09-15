import ast
import threading
from pathlib import Path

import pytest

from openpilot.selfdrive.selfdrived.follow_distance import FollowDistanceSetting


class Params:
  value = 1

  def get(self, key, return_default=True):
    assert key == 'LongitudinalPersonality'
    return self.value

  def put(self, key, value):
    assert key == 'LongitudinalPersonality'
    self.value = value


def test_read_started_before_button_cannot_restore_old_gap():
  setting, params = FollowDistanceSetting(1), Params()
  entered, release = threading.Event(), threading.Event()

  def stale_read(*args, **kwargs):
    old = params.value
    entered.set()
    assert release.wait(2)
    return old

  params.get = stale_read
  worker = threading.Thread(target=setting.sync, args=(params,))
  worker.start()
  try:
    assert entered.wait(1)
    assert setting.cycle() == 0
  finally:
    release.set()
    worker.join(2)
  assert setting.snapshot() == 0
  del params.get
  assert setting.sync(params)
  assert setting.snapshot() == params.value == 0


def test_slow_write_does_not_block_buttons_or_lose_latest_request():
  setting, params = FollowDistanceSetting(1), Params()
  entered, release, clicked = threading.Event(), threading.Event(), threading.Event()

  def slow_put(key, value):
    entered.set()
    assert release.wait(2)
    params.value = value

  params.put = slow_put
  assert setting.cycle() == 0
  worker = threading.Thread(target=setting.sync, args=(params,))
  worker.start()
  assert entered.wait(1)

  def buttons():
    assert setting.cycle() == 2
    assert setting.cycle() == 1
    clicked.set()

  caller = threading.Thread(target=buttons)
  caller.start()
  try:
    assert clicked.wait(1), 'button handler blocked on persistent storage'
  finally:
    release.set()
    caller.join(2)
    worker.join(2)
  assert setting.snapshot() == 1
  assert params.value == 0
  del params.put
  assert setting.sync(params)
  assert setting.snapshot() == params.value == 1


@pytest.mark.parametrize('value', [None, -1, 3, True, b'1'])
def test_bad_persisted_values_do_not_enter_published_enum(value):
  setting, params = FollowDistanceSetting(1), Params()
  params.value = value
  assert not setting.sync(params)
  assert setting.snapshot() == 1


def test_external_settings_are_accepted_after_pending_button_is_saved():
  setting, params = FollowDistanceSetting(1), Params()
  assert setting.cycle() == 0
  assert setting.sync(params)
  params.value = 2
  assert setting.sync(params)
  assert setting.snapshot() == 2


def test_unacknowledged_write_preserves_request_and_retries():
  setting, params = FollowDistanceSetting(1), Params()
  params.put = lambda key, value: None
  assert setting.cycle() == 0
  assert not setting.sync(params)
  assert setting.snapshot() == 0
  del params.put
  assert setting.sync(params)
  assert params.value == 0


def test_production_button_path_has_no_disk_writes_and_worker_owns_sync():
  tree = ast.parse((Path(__file__).parents[1] / 'selfdrived.py').read_text())
  methods = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
  update = ast.unparse(methods['update_events'])
  assert 'self.follow_distance.cycle()' in update
  assert 'LongitudinalPersonality' not in update
  assert 'self.follow_distance.snapshot()' in ast.unparse(methods['step'])
  assert 'self.follow_distance.sync(self.params)' in ast.unparse(methods['params_thread'])
