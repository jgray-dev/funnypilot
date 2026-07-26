"""FunnyPilot v3.4.4 — /dev/shm brake-lamp channel.

Import-light: the module is stdlib-only by design, so this runs anywhere.

The invariant that matters is the one the status dot leans on: an absent or
stale channel must read as None (UNKNOWN), never as False. False means "the
car told us its brakes are off"; None means "nobody told us anything". Collapse
those two and a dead publisher silently becomes a confident, wrong answer.
"""
import os
import time

import pytest

from openpilot.sunnypilot.selfdrive.car import brake_light_shm as m


@pytest.fixture(autouse=True)
def shm_path(tmp_path, monkeypatch):
  monkeypatch.setattr(m, 'SHM_PATH', str(tmp_path / 'fp_brake'))
  return m.SHM_PATH


class TestRoundTrip:
  def test_missing_file_is_unknown(self):
    assert m.read_brake_light() is None

  def test_write_true_reads_true(self):
    m._write(True)
    assert m.read_brake_light() is True

  def test_write_false_reads_false(self):
    m._write(False)
    assert m.read_brake_light() is False

  def test_transition_is_visible(self):
    m._write(False)
    assert m.read_brake_light() is False
    m._write(True)
    assert m.read_brake_light() is True

  def test_garbage_is_unknown(self, shm_path):
    with open(shm_path, 'w') as f:
      f.write('banana')
    assert m.read_brake_light() is None

  def test_empty_is_unknown(self, shm_path):
    with open(shm_path, 'w') as f:
      f.write('')
    assert m.read_brake_light() is None

  def test_stale_file_is_unknown_not_false(self, shm_path):
    m._write(False)
    old = time.clock_gettime(time.CLOCK_REALTIME) - (m.STALE_S + 5.0)
    os.utime(shm_path, (old, old))
    assert m.read_brake_light() is None

  def test_stale_true_is_also_unknown(self, shm_path):
    # a stale lit lamp must not pin the dot red forever either
    m._write(True)
    old = time.clock_gettime(time.CLOCK_REALTIME) - (m.STALE_S + 5.0)
    os.utime(shm_path, (old, old))
    assert m.read_brake_light() is None


class TestPublisherRate:
  def test_first_call_publishes(self):
    p = m.BrakeLightPublisher()
    p.update(False)
    assert m.read_brake_light() is False

  def test_transition_publishes_immediately(self, shm_path):
    p = m.BrakeLightPublisher()
    p.update(False)
    os.unlink(shm_path)          # prove the NEXT write is the transition's
    p.update(True)
    assert m.read_brake_light() is True

  def test_steady_state_does_not_write_every_frame(self, shm_path):
    p = m.BrakeLightPublisher()
    p.update(False)
    os.unlink(shm_path)
    for _ in range(m.HEARTBEAT_FRAMES - 1):
      p.update(False)
    assert not os.path.exists(shm_path)   # rate-limited, as intended

  def test_heartbeat_refreshes_before_staleness(self, shm_path):
    p = m.BrakeLightPublisher()
    p.update(False)
    os.unlink(shm_path)
    for _ in range(m.HEARTBEAT_FRAMES):
      p.update(False)
    assert os.path.exists(shm_path)

  def test_heartbeat_period_stays_under_stale_window(self):
    # the 100 Hz CarState rate is what makes the heartbeat meaningful; if these
    # drift apart a steady state would read as UNKNOWN between heartbeats
    assert m.HEARTBEAT_FRAMES / 100.0 < m.STALE_S

  def test_publisher_never_raises_on_a_bad_path(self, monkeypatch):
    monkeypatch.setattr(m, 'SHM_PATH', '/nonexistent-dir-fp/fp_brake')
    p = m.BrakeLightPublisher()
    p.update(True)   # must not raise — a telemetry failure can't touch control
    assert m.read_brake_light() is None
