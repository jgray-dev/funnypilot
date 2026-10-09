#!/usr/bin/env python3
import os
import shutil
import threading
import time
from pathlib import Path
from openpilot.system.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr
from openpilot.system.loggerd import drive_retention
from openpilot.sunnypilot.harvest import retention as harvest

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 15
RECOVER_PERCENT = 20
RECOVER_BYTES = 7 * 1024**3
PRESSURE_POLL_SECONDS = 1.0

DELETE_LAST = ['boot', 'crash']

PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'
PRESERVE_COUNT = 5


def has_preserve_xattr(d: str) -> bool:
  return getxattr(os.path.join(Paths.log_root(), d), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE


def get_preserved_segments(dirs_by_creation: list[str]) -> set[str]:
  # skip deleting most recent N preserved segments (and their prior segment)
  preserved = set()
  for n, d in enumerate(filter(has_preserve_xattr, reversed(dirs_by_creation))):
    if n == PRESERVE_COUNT:
      break
    date_str, _, seg_str = d.rpartition("--")

    # ignore non-segment directories
    if not date_str:
      continue
    try:
      seg_num = int(seg_str)
    except ValueError:
      continue

    # preserve segment and two prior
    for _seg_num in range(max(0, seg_num - 2), seg_num + 1):
      preserved.add(f"{date_str}--{_seg_num}")

  return preserved


def _clean_one(root, saved, low_space, preserved=(), archive_root=None, pinned=(), harvesting=False):
  dirs = listdir_by_creation(root)
  expired = drive_retention.expired_routes(root, dirs)
  if harvesting:
    expired -= harvest.awaiting_harvest(root, dirs, expired)
  # Under pressure, spend what the cloud already has before what it lacks.
  def delete_order(d):
    return (d in DELETE_LAST, d in preserved, harvesting and low_space and harvest.unharvested_last(root, d))
  for name in sorted(dirs, key=delete_order):
    route = drive_retention.segment_route(name)
    if name in pinned or route in saved or (not low_space and route not in expired):
      continue
    path = os.path.join(root, name)
    if os.path.islink(path) or drive_retention.is_locked(path):
      continue
    # Expired recordings are removed, not shuffled onto another disk forever.
    if archive_root is not None and route not in expired:
      target = os.path.join(archive_root, name)
      try:
        if os.path.lexists(target):
          # Never nest a segment into an existing segment or follow a symlink.
          continue
        cloudlog.warning(f"moving {path} to {target}")
        shutil.move(path, target)
        return True
      except OSError:
        cloudlog.exception(f"issue moving {path} to {target}")
        # Keep the source on a failed/partial copy; retry after the next scan.
        continue
    try:
      cloudlog.info(f"deleting {path}")
      shutil.rmtree(path)
      return True
    except OSError:
      cloudlog.exception(f"issue deleting {path}")
  return False


def cleanup_once(pressure=None, expire=True):
  root = Paths.log_root()
  if not os.path.isdir(root):
    return False
  # One lock and one saved set for both disks. It also serializes manual web
  # deletion; a saved route is a hard exclusion, even below the free-space floor.
  with drive_retention.locked_saved(root) as saved:
    pinned = drive_retention.pinned_segments(root)
    def pressured(kind):
      # Hysteresis prevents hovering at the no-entry boundary. Persistent state
      # belongs to the deleter thread, never the control/hardware publisher.
      active = pressure is not None and pressure.get(kind, False)
      low = (get_available_bytes(default=RECOVER_BYTES+1, path_type=kind) < (RECOVER_BYTES if active else MIN_BYTES) or
             get_available_percent(default=RECOVER_PERCENT+1, path_type=kind) < (RECOVER_PERCENT if active else MIN_PERCENT))
      if pressure is not None:
        pressure[kind] = low
      return low
    low_space = pressured('internal')
    harvesting = harvest.linked()
    changed = False
    archive_root = None
    external = Paths.log_root_external()
    if Path(external).is_mount():
      low_external = pressured('external')
      changed = _clean_one(external, saved, low_external, pinned=pinned) if low_external or expire else False
      # Only archive when the external disk has room. Its saved data is never
      # evicted to make space for an internal unsaved recording.
      if (get_available_bytes(default=0, path_type="external") >= MIN_BYTES and
          get_available_percent(default=0, path_type="external") >= MIN_PERCENT):
        archive_root = external
    if not low_space and not expire:
      return changed
    preserved = get_preserved_segments(listdir_by_creation(root)) if low_space else set()
    return _clean_one(root, saved, low_space, preserved, archive_root, pinned, harvesting) or changed


def deleter_thread(exit_event: threading.Event):
  pressure = {}
  next_expiry = 0.0
  next_error_log = 0.0
  while not exit_event.is_set():
    try:
      now = time.monotonic()
      changed = cleanup_once(pressure, expire=now >= next_expiry)
      if not changed and now >= next_expiry:
        next_expiry = now + drive_retention.CLEANUP_INTERVAL
    except (OSError, ValueError):
      # Corrupt/unreadable save metadata must never silently mean "none saved".
      if time.monotonic() >= next_error_log:
        cloudlog.exception("drive cleanup paused: cannot read retention state")
        next_error_log = time.monotonic() + 60.0
      changed = False
    # Poll free space every second, including when everything was protected.
    # No extra IPC reader, and no filesystem work in the driving health loop.
    exit_event.wait(.1 if changed else PRESSURE_POLL_SECONDS)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()
