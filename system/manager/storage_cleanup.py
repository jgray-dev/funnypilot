"""FunnyPilot v3.4.5: bounded, best-effort disk reclamation at manager startup.

WHY THIS EXISTS: a v3.4.4 flash briefly showed "storage full". The stock
cleaner (`system/loggerd/deleter.py`) only ever touches `Paths.log_root()`,
so anything that grows OUTSIDE the drive log tree accumulates forever. The two
known offenders on this fork are both created by things we do a lot of:

  1. `/data/safe_staging/old_openpilot` — a FULL copy of the previous
     /data/openpilot, moved aside by launch_chffrplus.sh during an overlay
     update swap. Nothing ever removes it. It is also load-bearing in the
     wrong direction: launch_chffrplus.sh refuses to install any future
     update while it exists ("openpilot backup found, not updating"). So
     removing it is correct on its own merits and is done UNCONDITIONALLY.
  2. `.git` bloat in /data/openpilot. Every deploy is `git fetch` +
     `reset --hard` onto a force-pushed branch, which orphans the previous
     tip. Those objects stay reachable through the reflog and are never
     packed away. Reclaimed only when space is actually low, because gc
     costs real time at boot.

DESIGN RULES, all load-bearing:
  - stdlib only, and NOTHING here may raise. This module is imported and
    called from `manager_init()`; an exception here is a car that does not
    start. Every step is individually try/excepted and the top-level entry
    point catches BaseException-minus-the-ones-you-must-not-swallow.
  - It runs on a DAEMON THREAD. Boot must not wait on `du`/`git gc`.
  - It NEVER touches: /data/openpilot itself, /data/params, the loggerd log
    root (deleter owns that and has its own retention policy), or
    /data/safe_staging/merged (an active overlayfs MOUNT — `rm -rf` on the
    parent while it is mounted is how you delete the running system).
  - Deletion targets are an explicit ALLOW-LIST of absolute paths. There is
    no globbing, no walking-and-deciding, no "delete files older than N".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

# Absolute paths only. Anything not on this list is never removed.
OLD_OPENPILOT = "/data/safe_staging/old_openpilot"
BASEDIR = "/data/openpilot"

# Paths cleaned only when free space is below LOW_BYTES/LOW_PERCENT. Each is
# regenerated on demand by whatever wrote it; none is needed to boot.
LOW_SPACE_TARGETS = (
  "/data/core",              # crash dumps
  "/tmp/comma_download_cache",
)

# `deleter.py` maintains 5 GB / 10% free by trimming drive segments. We aim a
# little above it so this pass does work BEFORE deleter starts eating logs the
# user may still want, not after.
LOW_BYTES = 6 * 1024 ** 3
LOW_PERCENT = 12.0

# Below this, .git isn't worth the gc time.
GIT_GC_MIN_BYTES = 512 * 1024 ** 2
GIT_GC_TIMEOUT_S = 180

# Bound the size walk so a pathological tree can't spin the thread.
_MAX_WALK_ENTRIES = 200_000


def free_space(path: str = "/data") -> tuple[int, float]:
  """(free bytes, free percent). (-1, -1.0) if it can't be determined."""
  try:
    st = os.statvfs(path)
    if st.f_blocks <= 0:
      return -1, -1.0
    return st.f_bavail * st.f_frsize, 100.0 * st.f_bavail / st.f_blocks
  except Exception:
    return -1, -1.0


def dir_size(path: str) -> int:
  """Apparent size of `path`, in bytes. 0 if missing/unreadable.

  Uses lstat and never follows symlinks — a symlink into the log tree must
  not make this report (or a caller delete) something enormous.
  """
  total = 0
  entries = 0
  try:
    stack = [path]
    while stack:
      cur = stack.pop()
      with os.scandir(cur) as it:
        for e in it:
          entries += 1
          if entries > _MAX_WALK_ENTRIES:
            return total
          try:
            if e.is_dir(follow_symlinks=False):
              stack.append(e.path)
            else:
              total += e.stat(follow_symlinks=False).st_size
          except OSError:
            continue
  except Exception:
    pass
  return total


def _rm(path: str) -> int:
  """Remove a file or tree. Returns bytes reclaimed (0 on any failure)."""
  if not os.path.exists(path):
    return 0
  size = dir_size(path) if os.path.isdir(path) else 0
  try:
    if os.path.isdir(path) and not os.path.islink(path):
      shutil.rmtree(path, ignore_errors=True)
    else:
      size = os.path.getsize(path)
      os.remove(path)
  except Exception:
    return 0
  return 0 if os.path.exists(path) else size


def _git_gc(basedir: str = BASEDIR) -> int:
  """Expire the reflog and repack. Returns bytes reclaimed.

  NEVER run under sudo (root-owned files inside .git are what broke the
  v3.4.3 deploy). A gc that fails or times out is a no-op, not an error.
  """
  git_dir = os.path.join(basedir, ".git")
  before = dir_size(git_dir)
  if before < GIT_GC_MIN_BYTES:
    return 0
  env = dict(os.environ)
  env["GIT_TERMINAL_PROMPT"] = "0"
  for cmd in (["git", "-C", basedir, "reflog", "expire", "--expire=now", "--all"],
              ["git", "-C", basedir, "gc", "--prune=now", "--quiet"]):
    try:
      subprocess.run(cmd, env=env, timeout=GIT_GC_TIMEOUT_S,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
      return 0
  return max(0, before - dir_size(git_dir))


def cleanup(log=None) -> dict:
  """Run one cleanup pass. Returns a summary dict; never raises."""
  report: dict = {"reclaimed": 0, "steps": {}}

  def note(name: str, n: int) -> None:
    report["steps"][name] = n
    report["reclaimed"] += n

  try:
    free_b, free_p = free_space()
    report["free_before"] = free_b
    report["free_percent_before"] = free_p

    # Unconditional: it is dead weight AND it blocks every future update.
    note("old_openpilot", _rm(OLD_OPENPILOT))

    low = (0 <= free_b < LOW_BYTES) or (0 <= free_p < LOW_PERCENT)
    report["low"] = low
    if low:
      for p in LOW_SPACE_TARGETS:
        note(os.path.basename(p) or p, _rm(p))
      note("git_gc", _git_gc())

    report["free_after"] = free_space()[0]
  except Exception as e:  # pragma: no cover - belt and braces
    report["error"] = repr(e)

  if log is not None and report.get("reclaimed"):
    try:
      log(report)
    except Exception:
      pass
  return report


def cleanup_async(log=None) -> threading.Thread | None:
  """Kick off `cleanup()` on a daemon thread. Never raises, never blocks."""
  try:
    t = threading.Thread(target=cleanup, args=(log,), name="storage_cleanup", daemon=True)
    t.start()
    return t
  except Exception:
    return None
