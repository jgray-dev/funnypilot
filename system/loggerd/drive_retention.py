"""Persistent whole-route saves shared by the web UI and loggerd's deleter.

Metadata lives beside the segment directories, outside the git checkout.
The process lock covers save changes AND deletion: a successful save response
cannot race a cleaner that already selected that drive. Unreadable metadata
fails closed. This is separate from user.preserve, which is only delete-last.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import re
import tempfile
import time

RETENTION_DAYS = 7
CLEANUP_INTERVAL = 60.0
_MANIFEST = '.fp_saved_drives.json'
_LOCK = '.fp_drive_retention.lock'
_ROUTE = re.compile(r'[A-Za-z0-9|_-]{1,128}')
_MAX_METADATA = 1024 * 1024


class SavedDriveError(ValueError):
  pass


class ActiveDriveError(ValueError):
  pass


def validate_route(route):
  if not isinstance(route, str) or not _ROUTE.fullmatch(route):
    raise ValueError('bad route name')


def segment_route(name):
  route, sep, segment = name.rpartition('--')
  return route if sep and segment.isascii() and segment.isdigit() and _ROUTE.fullmatch(route) else None


def read_saved(root):
  try:
    fd = os.open(os.path.join(root, _MANIFEST), os.O_RDONLY | os.O_NOFOLLOW)
  except FileNotFoundError:
    return set()
  with os.fdopen(fd, 'r') as f:
    raw = f.read(_MAX_METADATA + 1)
  if len(raw) > _MAX_METADATA:
    raise ValueError('saved drive metadata is too large')
  data = json.loads(raw)
  if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('routes'), list):
    raise ValueError('invalid saved drive metadata')
  for route in data['routes']:
    validate_route(route)
  return set(data['routes'])


@contextmanager
def locked_saved(root):
  # A plain file, so uploader/listdir_by_creation never treats it as a segment.
  fd = os.open(os.path.join(root, _LOCK), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
  with os.fdopen(fd, 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    yield read_saved(root)


def write_saved(root, routes):
  """Caller holds locked_saved. Replace atomically and persist before replying."""
  raw = json.dumps({'version': 1, 'routes': sorted(routes)}) + '\n'
  if len(raw) > _MAX_METADATA:
    raise ValueError('saved drive metadata is too large')
  fd, tmp = tempfile.mkstemp(prefix='.fp_saved_', dir=root)
  try:
    with os.fdopen(fd, 'w') as f:
      f.write(raw)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, os.path.join(root, _MANIFEST))
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(directory)
    finally:
      os.close(directory)
  finally:
    if os.path.exists(tmp):
      os.unlink(tmp)


def is_locked(path):
  try:
    return any(name.endswith('.lock') for name in os.listdir(path))
  except OSError:
    return True


def expired_routes(root, dirs, now=None):
  """Age the newest segment of each route; never age out the newest route.

  Filesystem mtimes include recording activity and avoid relying on a route
  name's clock. A future/invalid device clock postpones expiry. No logs parsed.
  Locked routes are excluded in full, including earlier unlocked segments.
  """
  newest = {}
  active = set()
  for name in dirs:
    route = segment_route(name)
    path = os.path.join(root, name)
    if route is None or os.path.islink(path):
      continue
    try:
      newest[route] = max(newest.get(route, 0), os.stat(path).st_mtime)
    except OSError:
      active.add(route)
    if is_locked(path):
      active.add(route)
  if not newest:
    return set()
  active.add(max(newest, key=newest.get))
  # Filesystem mtime is wall clock, including across reboots; monotonic is
  # only appropriate for scheduling the cleanup loop, not comparing mtimes.
  cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400  # noqa: TID251
  return {route for route, modified in newest.items() if 0 < modified < cutoff and route not in active}
