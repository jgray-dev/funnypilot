"""Bounded control internals for feedback, without another IPC subscriber.

Only /dev/shm IO; imports and construction are inert. Diagnostics never change
controller state. The recorder attaches the original monotonic timestamp.
"""
import json
import math
import os

PATH = '/dev/shm/fp_control_diagnostics.json'
MAX_BYTES = 8192


class ControlTap:
  def __init__(self, path=PATH):
    self.path = path

  def publish(self, row):
    try:
      data = json.dumps(row, allow_nan=False, separators=(',', ':'))
      if len(data) > MAX_BYTES:
        return
      with open(self.path + '.tmp', 'w') as stream:
        stream.write(data)
      os.replace(self.path + '.tmp', self.path)
    except Exception:
      pass


def read_control(now, path=PATH):
  try:
    with open(path) as stream:
      raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
      return None
    row = json.loads(raw)
    stamp = row.get('t')
    if type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= now - stamp <= .1:
      return row
  except (OSError, ValueError, TypeError, AttributeError):
    pass
  return None
