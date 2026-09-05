"""Small, import-light messages; no disk/network work at import or construction."""
import json
import os
import socket
import time

ROOT = '/data/funnypilot_feedback'
SOCKET = '/dev/shm/fp_feedback.sock'
STATUS = '/dev/shm/fp_feedback_status.json'
CORNER_CONTEXT = '/dev/shm/fp_feedback_corner.json'
CORNER_RULES = '/dev/shm/fp_feedback_rules.json'
LABELS = {
  'steering_bite': 'Steering bite',
  'steering_wander': 'Steering wander',
  'unnecessary_slowdown': 'Unneeded slowdown',
  'late_braking': 'Late braking',
  'harsh_braking': 'Harsh braking',
  'slow_response': 'Slow response',
}


def _invalid_number(value):
  raise ValueError('nonfinite JSON number')


def read_json(path, default=None, limit=65536):
  try:
    with open(path) as f:
      data = f.read(limit + 1)
    return json.loads(data, parse_constant=_invalid_number) if len(data) <= limit else default
  except (OSError, ValueError):
    return default


def atomic_json(path, data, durable=False):
  # Single owner per file (feedbackd or plannerd). Files on /data are fsynced;
  # telemetry in /dev/shm never blocks a control loop on durable storage.
  tmp = path + '.tmp'
  with open(tmp, 'w') as f:
    json.dump(data, f, allow_nan=False, separators=(',', ':'))
    f.flush()
    if durable:
      os.fsync(f.fileno())
  os.replace(tmp, path)
  if durable:
    directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(directory)
    finally:
      os.close(directory)


def send_report(event_id, labels, started):
  try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
      s.setblocking(False)
      s.sendto(json.dumps({'id': event_id, 'labels': labels, 'started': started,
                           'sent': time.monotonic()}).encode(), SOCKET)
    return True
  except (OSError, ValueError):
    return False
