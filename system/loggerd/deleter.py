#!/usr/bin/env python3
import os
import shutil
import threading
from pathlib import Path
from openpilot.system.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr
from openpilot.system.loggerd import drive_retention

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 10

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


def _clean_one(root, saved, low_space, preserved=(), archive_root=None):
  dirs = listdir_by_creation(root)
  expired = drive_retention.expired_routes(root, dirs)
  for name in sorted(dirs, key=lambda d: (d in DELETE_LAST, d in preserved)):
    route = drive_retention.segment_route(name)
    if route in saved or (not low_space and route not in expired):
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


def cleanup_once():
  root = Paths.log_root()
  if not os.path.isdir(root):
    return False
  # One lock and one saved set for both disks. It also serializes manual web
  # deletion; a saved route is a hard exclusion, even below the free-space floor.
  with drive_retention.locked_saved(root) as saved:
    low_space = (get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES or
                 get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT)
    changed = False
    archive_root = None
    external = Paths.log_root_external()
    if Path(external).is_mount():
      low_external = (get_available_bytes(default=MIN_BYTES + 1, path_type="external") < MIN_BYTES or
                      get_available_percent(default=MIN_PERCENT + 1, path_type="external") < MIN_PERCENT)
      changed = _clean_one(external, saved, low_external)
      # Only archive when the external disk has room. Its saved data is never
      # evicted to make space for an internal unsaved recording.
      if (get_available_bytes(default=0, path_type="external") >= MIN_BYTES and
          get_available_percent(default=0, path_type="external") >= MIN_PERCENT):
        archive_root = external
    preserved = get_preserved_segments(listdir_by_creation(root)) if low_space else set()
    return _clean_one(root, saved, low_space, preserved, archive_root) or changed


def deleter_thread(exit_event: threading.Event):
  while not exit_event.is_set():
    try:
      changed = cleanup_once()
    except (OSError, ValueError):
      # Corrupt/unreadable save metadata must never silently mean "none saved".
      cloudlog.exception("drive cleanup paused: cannot read retention state")
      changed = False
    # Drain eligible data one segment at a time. If everything is saved/locked,
    # wait a full interval instead of busy-looping on an unreclaimable disk.
    exit_event.wait(.1 if changed else drive_retention.CLEANUP_INTERVAL)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()
