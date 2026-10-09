"""Resumable, checksummed upload of one loggerd segment to the drives Worker."""
import hashlib
import os
from urllib.parse import quote

import requests

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.harvest.segments import segment_files, mark_harvested

PART = 16 * 1024 * 1024
MAX_FILE = 1024 * 1024 * 1024
TIMEOUT = (5, 60)


class Conflict(Exception):
  """The cloud already holds a complete, different copy of this segment."""


def describe_file(path, size):
  """Hash exactly `size` bytes (snapshot of a closed file), in PART chunks."""
  parts, whole, left = [], hashlib.sha256(), size
  with open(path, 'rb') as f:
    while left > 0:
      data = f.read(min(PART, left))
      if not data:
        raise RuntimeError('segment file shrank while hashing')
      left -= len(data)
      whole.update(data)
      parts.append({'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
  return {'size': size, 'sha256': whole.hexdigest(), 'parts': parts}


def build_manifest(route, segment, path, identity):
  artifacts, sizes = [], {}
  for name in segment_files(path):
    size = os.stat(os.path.join(path, name)).st_size
    if size > MAX_FILE:
      continue  # visibly absent from the manifest rather than truncated
    artifacts.append({'name': name, **describe_file(os.path.join(path, name), size)})
    sizes[name] = size
  if not artifacts:
    return None
  return {'route': route, 'segment': segment, 'created_at': os.stat(path).st_mtime, 'commit': identity.get('commit', ''),
          'branch': identity.get('branch', ''), 'version': identity.get('version', ''), 'dirty': bool(identity.get('dirty')),
          'artifacts': artifacts}


def upload_segment(route, segment, path, config, identity, allowed=lambda: True):
  """Returns True once the cloud acknowledged the segment and it is marked."""
  endpoint = config.get('endpoint', '')
  if not endpoint.startswith('https://') or not config.get('token'):
    raise ValueError('Harvest upload is not linked')
  if not allowed():
    return False
  manifest = build_manifest(route, segment, path, identity)
  if manifest is None:
    return False
  base = f"{endpoint.rstrip('/')}/segments/{quote(route, safe='')}/{segment}"
  with requests.Session() as session:
    session.headers['Authorization'] = 'Bearer ' + config['token']

    def send(method, url, **kwargs):
      if not allowed():
        raise RuntimeError('Harvest paused until parked on Wi-Fi')
      response = session.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs)
      if response.status_code == 409 and url == base:
        raise Conflict()
      response.raise_for_status()
      if not response.json().get('ok'):
        raise RuntimeError('Cloud did not acknowledge upload')

    try:
      send('PUT', base, json=manifest)
    except Conflict:
      mark_harvested(path)  # a complete copy exists; never loop on it
      return True
    for artifact in manifest['artifacts']:
      with open(os.path.join(path, artifact['name']), 'rb') as f:
        for index, part in enumerate(artifact['parts']):
          data = f.read(part['size'])
          if len(data) != part['size'] or hashlib.sha256(data).hexdigest() != part['sha256']:
            raise RuntimeError('Segment changed since it was hashed')
          send('PUT', f"{base}/files/{artifact['name']}/{index}", data=data)
    send('POST', base + '/complete')
  mark_harvested(path)
  return True


def write_status(path, status):
  P.atomic_json(path, status)
