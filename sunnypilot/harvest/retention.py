"""Harvest-aware retention decisions for loggerd's deleter (pure, no native imports).

Free-space pressure always wins: the training archive never costs engagement or
recording headroom. Harvesting only changes (1) expiry, which waits for the
cloud copy for a bounded grace period, and (2) deletion order under pressure,
which spends segments the cloud already has before those it lacks.
"""
import os
import time

from openpilot.sunnypilot.harvest import segments as S
from openpilot.system.loggerd import drive_retention as R

GRACE_DAYS = 30


def linked(config_path=S.CONFIG):
  return os.path.isfile(config_path)


def awaiting_harvest(root, dirs, routes, now=None):
  """Routes in `routes` with a recent segment the cloud has not acknowledged."""
  if not routes:
    return set()
  cutoff = (time.time() if now is None else now) - GRACE_DAYS * 86400  # noqa: TID251 - filesystem mtimes
  held = set()
  for name in dirs:
    route = R.segment_route(name)
    if route in routes and route not in held:
      path = os.path.join(root, name)
      try:
        if os.stat(path).st_mtime > cutoff and not S.is_harvested(path):
          held.add(route)
      except OSError:
        held.add(route)  # cannot tell: keep it this round
  return held


def unharvested_last(root, name):
  """Sort component: False (already in the cloud) deletes first."""
  return not S.is_harvested(os.path.join(root, name))
