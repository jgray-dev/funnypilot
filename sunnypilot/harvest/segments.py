"""Which closed loggerd segments still need harvesting, and the durable mark.

The mark is an xattr on the segment directory, so it shares the segment's
lifetime: when loggerd's deleter removes the segment the mark goes with it, and
there is no separate ledger to corrupt or grow. Reads here are deliberately
uncached (system.loggerd.xattr_cache never sees another process's writes).
"""
import errno
import os
import time

from openpilot.system.loggerd import drive_retention as R

HARVEST_DIR = '/data/funnypilot_harvest'
CONFIG = os.path.join(HARVEST_DIR, 'cloud.json')
MARK = 'user.fp_harvested'
# Road + wide-road video and full-rate logs are the training data. Cabin camera
# and audio are never listed, never hashed and never uploaded.
FILES = ('fcamera.hevc', 'ecamera.hevc', 'rlog.zst', 'qlog.zst', 'qcamera.ts')
QUIET_S = 30.0  # a segment still being written is never harvested


def is_harvested(path):
  try:
    return os.getxattr(path, MARK, follow_symlinks=False) == b'1'
  except OSError as e:
    if e.errno in (errno.ENODATA, errno.ENOENT):
      return False
    # Unsupported filesystem or unreadable: claim "not harvested", so nothing
    # is ever deleted early on the strength of a mark we could not read.
    return False


def mark_harvested(path):
  os.setxattr(path, MARK, b'1', follow_symlinks=False)


def segment_files(path):
  """Existing, regular, non-empty harvestable files in a segment directory."""
  found = []
  for name in FILES:
    p = os.path.join(path, name)
    try:
      st = os.lstat(p)
    except OSError:
      continue
    if os.path.isfile(p) and not os.path.islink(p) and st.st_size > 0:
      found.append(name)
  return found


def pending_segments(root, now=None):
  """Oldest-first (route, segment, path) for closed, unharvested segments."""
  now = time.time() if now is None else now  # noqa: TID251 - compared with filesystem mtimes
  out = []
  try:
    names = sorted(os.listdir(root))
  except OSError:
    return out
  for name in names:
    route = R.segment_route(name)
    path = os.path.join(root, name)
    if route is None or os.path.islink(path) or not os.path.isdir(path):
      continue
    if R.is_locked(path) or is_harvested(path):
      continue
    try:
      newest = max([os.stat(path).st_mtime] + [os.stat(os.path.join(path, f)).st_mtime for f in segment_files(path)])
    except OSError:
      continue
    if now - newest < QUIET_S or not segment_files(path):
      continue
    out.append((route, int(name.rpartition('--')[2]), path))
  # Route order is creation order; sort segments numerically within a route.
  out.sort(key=lambda t: (os.stat(t[2]).st_mtime, t[0], t[1]))
  return out
