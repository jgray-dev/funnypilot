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
  2. REMOVED IN v3.4.6 — `.git` bloat in /data/openpilot. v3.4.5 ran
     `git reflog expire` + `git gc --prune=now` from here. That is the
     memory leak described in the v3.4.6 changelog and it is not coming
     back; see NO SUBPROCESSES below for the full reasoning.

DESIGN RULES, all load-bearing:
  - stdlib only, and NOTHING here may raise. This module is imported and
    called from `manager_init()`; an exception here is a car that does not
    start. Every step is individually try/excepted and the top-level entry
    point catches BaseException-minus-the-ones-you-must-not-swallow.
  - It runs on a DAEMON THREAD. Boot must not wait on the size walk.
  - NO SUBPROCESSES (v3.4.6). Everything this module does must be bounded in
    MEMORY as well as in time, because it runs concurrently with the car
    going onroad on a device with no swap.
    `git gc` is the counter-example that forced this rule. Measured on the
    funnypilot repo (423 MB of packs, 4 cores): `git gc --prune=now` peaks at
    1.69 GB RSS — `git repack` spawns one `pack-objects` per core and the
    delta window is unbounded by default. On a 4 GB device already running
    the onroad stack that is an OOM, and it happens seconds after
    `manager_init()`, i.e. exactly as the driver pulls away.
    Three further reasons it can never live here, any one of them sufficient:
      * `subprocess.run(timeout=...)` kills only the direct child. `git gc`'s
        `repack`/`pack-objects` grandchildren survive the timeout and keep
        allocating, so GIT_GC_TIMEOUT_S bounded nothing at all.
      * upstream openpilot DELIBERATELY disables on-device gc —
        `system/updated/updated.py:setup_git_options` sets `gc.auto=0` and
        `gc.autoDetach=false`. Re-adding it by hand overrides a decision that
        was made for this exact reason.
      * rewriting `.git` makes `updated.py:init_overlay` see
        `find .git -newer .overlay_init` non-empty, so it tears down and
        rebuilds the whole overlay on the next boot — which costs more disk
        than the gc reclaimed. It made the storage problem worse.
    Reclaiming `.git` is offroad maintenance, not a boot task. A device whose
    `.git` has actually run away wants a fresh clone.
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
import threading

# Absolute paths only. Anything not on this list is never removed.
OLD_OPENPILOT = "/data/safe_staging/old_openpilot"

# Paths cleaned only when free space is below LOW_BYTES/LOW_PERCENT. Each is
# regenerated on demand by whatever wrote it; none is needed to boot.
LOW_SPACE_TARGETS = (
  "/data/core",              # crash dumps
  "/tmp/comma_download_cache",
)

# `deleter.py` maintains 5 GB / 10% free by trimming drive segments. We aim a
# little above it so this pass does work BEFORE deleter starts eating logs the
# user may still want, not after.
#
# NOTE, and it is the reason v3.4.5's leak fired on every single ignition
# cycle rather than occasionally: because these sit ABOVE deleter's floor,
# `low` is effectively ALWAYS TRUE on a device that has recorded any real
# mileage — deleter's steady state IS 5 GB / 10% free, so free space hovers
# just under 6 GB / 12% forever. That is harmless for the cheap rmtrees below
# and is why the thresholds are unchanged, but it means `low` must never be
# read as "rare". Anything expensive gated on it runs every boot.
LOW_BYTES = 6 * 1024 ** 3
LOW_PERCENT = 12.0

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
      # Only cheap, memory-bounded rmtrees may hang off this gate — see the
      # note on LOW_BYTES: `low` is true on essentially every boot.
      for p in LOW_SPACE_TARGETS:
        note(os.path.basename(p) or p, _rm(p))

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
