#!/usr/bin/env python3
"""Background drive harvester: uploads every closed segment while parked on Wi-Fi.

No messaging subscriptions (nothing here can touch msgq's reader budget); only
Params, the hardware's network type and the filesystem. Idle until
/data/funnypilot_harvest/cloud.json exists, so shipping this changes nothing on a
device that has not been provisioned. Runs below normal priority off the
control cores, and stops between requests the moment ignition comes on.
"""
import os
import subprocess
import time
from pathlib import Path

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.harvest import segments as S
from openpilot.sunnypilot.harvest.uploader import upload_segment, write_status

STATUS = '/dev/shm/fp_harvest_status.json'
GATE_S = 2.0
IDLE_S = 15.0
RETRY_S = 60.0
SKIP_S = 3600.0
MAX_FAILURES = 5


class HarvestGate:
  """Parked (IsOnroad false) and on Wi-Fi. Any failure reads as not allowed."""

  def __init__(self, params, hardware, wifi, clock=time.monotonic):
    self.params, self.hardware, self.wifi, self.clock = params, hardware, wifi, clock
    self.allowed, self.next_check = False, 0.0

  def __call__(self):
    now = self.clock()
    if now >= self.next_check:
      self.next_check = now + GATE_S
      try:
        self.allowed = bool(not self.params.get_bool('IsOnroad') and self.hardware.get_network_type() == self.wifi)
      except Exception:
        self.allowed = False
    return self.allowed


def identity(repo):
  def git(*args):
    try:
      return subprocess.check_output(['git', '-c', f'safe.directory={repo}', '-C', str(repo), *args],
                                     timeout=3, stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.SubprocessError):
      return ''
  try:
    version = (repo / 'FUNNYPILOT_VERSION').read_text().strip()
  except OSError:
    version = ''
  return {'commit': git('rev-parse', 'HEAD'), 'branch': git('branch', '--show-current'), 'version': version,
          'dirty': bool(git('status', '--porcelain', '--untracked-files=no'))}


class Harvester:
  def __init__(self, root, config_path, gate, ident, clock=time.monotonic, upload=upload_segment, status_path=STATUS):
    self.root, self.config_path, self.gate, self.ident = root, config_path, gate, ident
    self.clock, self.upload, self.status_path = clock, upload, status_path
    self.failures, self.skip_until = {}, {}
    self.uploaded = self.bytes = 0
    self.error = ''
    self.retry_at = 0.0

  def step(self):
    """One unit of work; returns seconds to wait before the next call."""
    config = P.read_json(self.config_path, {})
    if not config.get('token'):
      return IDLE_S
    now = self.clock()
    if now < self.retry_at or not self.gate():
      return IDLE_S
    todo = [t for t in S.pending_segments(self.root) if self.skip_until.get(t[2], 0) <= now]
    self._status(len(todo))
    if not todo:
      return IDLE_S
    route, segment, path = todo[0]
    try:
      size = sum(os.stat(os.path.join(path, n)).st_size for n in S.segment_files(path))
      if self.upload(route, segment, path, config, self.ident, allowed=self.gate):
        self.uploaded += 1
        self.bytes += size
        self.failures.pop(path, None)
        self.error = ''
        return 0.0
    except Exception as e:
      self.error = type(e).__name__  # never persist credentials or response bodies
      self.failures[path] = self.failures.get(path, 0) + 1
      if self.failures[path] >= MAX_FAILURES:
        self.skip_until[path] = now + SKIP_S
        self.failures.pop(path)
      self.retry_at = now + RETRY_S
    return IDLE_S

  def _status(self, pending):
    try:
      write_status(self.status_path, {'pending': pending, 'uploaded': self.uploaded, 'bytes': self.bytes, 'error': self.error})
    except OSError:
      pass


def main():
  from openpilot.common.params import Params
  from openpilot.common.swaglog import cloudlog
  from openpilot.common.thread_config import configure_background_thread
  from openpilot.system.hardware import HARDWARE
  from openpilot.system.hardware.hw import Paths
  from cereal import log

  try:
    os.nice(19)
  except OSError:
    cloudlog.exception('harvest scheduling priority unavailable')
  configure_background_thread()
  gate = HarvestGate(Params(), HARDWARE, log.DeviceState.NetworkType.wifi)
  harvester = Harvester(Paths.log_root(), S.CONFIG, gate, identity(Path(__file__).resolve().parents[2]))
  while True:
    try:
      wait = harvester.step()
    except Exception:
      cloudlog.exception('harvest step failed')
      wait = RETRY_S
    time.sleep(wait)


if __name__ == '__main__':
  main()
