"""Retryable, private incident uploads. Invoked by one offroad/Wi-Fi worker."""
import hashlib
from pathlib import Path
import time
import requests

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.navd import drive_index as D
from openpilot.system.loggerd.drive_retention import is_locked

CHUNK = 4 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_EVENT = 64 * 1024 * 1024


def describe_file(path):
  parts, h = [], hashlib.sha256()
  with open(path, 'rb') as f:
    while data := f.read(CHUNK):
      h.update(data)
      parts.append({'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()})
  if not parts:
    parts = [{'size':0, 'sha256':h.hexdigest()}]
  return {'name':Path(path).name, 'size':sum(p['size'] for p in parts), 'sha256':h.hexdigest(), 'parts':parts}


def upload_event(event_dir, config, log_root=D.REALDATA_ROOT, allowed=lambda: True):
  directory = Path(event_dir)
  event = P.read_json(str(directory / 'event.json'))
  if not event or event.get('state') != 'queued':
    return False
  endpoint = config.get('endpoint', '')
  if not endpoint.startswith('https://') or not config.get('token'):
    raise ValueError('Cloud upload is not linked')
  files = {'telemetry.jsonl':directory / 'telemetry.jsonl'}
  if (directory / 'triage.jsonl').exists():
    files['triage.jsonl'] = directory / 'triage.jsonl'
  missing = []
  # Entire enclosing minute segments retain playable video and capnp logs.
  # Never parse/re-encode road video, and never upload cabin video or audio.
  n = event['segment_at_report']
  for seg in range(max(0, n-1), n+2):
    path = D.segment_path(event['route'], seg, log_root)
    if path is None or not Path(path).is_dir():
      missing.append(f'segment {seg}')
      continue
    if is_locked(path):
      return False  # immutable files only; revisit when loggerd closes them
    for name in ('qlog.zst', 'qcamera.ts'):
      p = Path(path) / name
      if p.is_file() and not p.is_symlink():
        files[f'segment-{seg}-{name}'] = p
      else:
        missing.append(f'segment {seg}: {name}')
  manifest = dict(event)
  manifest.pop('state', None)
  manifest['artifacts'] = []
  manifest['missing'] = missing
  total = 0
  paths = {}
  for name, path in files.items():
    if not path.exists():
      missing.append(name)
      continue
    size = path.stat().st_size
    if size > MAX_FILE or total + size > MAX_EVENT:
      missing.append(name + ' (size limit)')
      continue
    if not allowed():
      return False
    entry = describe_file(path)
    entry['name'] = name
    manifest['artifacts'].append(entry)
    paths[name] = path
    total += size
  # Persist the exact manifest once: a response lost after completion must be
  # retryable even if some route segments have since gone away.
  manifest_path = directory / 'manifest.json'
  old = P.read_json(str(manifest_path))
  if old is not None:
    manifest = old
  else:
    P.atomic_json(str(manifest_path), manifest, durable=True)
  with requests.Session() as session:
    session.headers['Authorization'] = 'Bearer ' + config['token']
    base = endpoint.rstrip('/') + '/events/' + event['id']
    def send(method, url, **kwargs):
      if not allowed():
        raise RuntimeError('Upload paused until parked on Wi-Fi')
      response = session.request(method, url, timeout=(5, 20), allow_redirects=False, **kwargs)
      response.raise_for_status()
      if not response.json().get('ok'):
        raise RuntimeError('Cloud did not acknowledge upload')
    send('PUT', base, json=manifest)
    for artifact in manifest['artifacts']:
      path = paths.get(artifact['name'])
      if path is None:
        raise RuntimeError('Protected capture file is missing')
      with open(path, 'rb') as f:
        for index, part in enumerate(artifact['parts']):
          data = f.read(CHUNK)
          if len(data) != part['size'] or hashlib.sha256(data).hexdigest() != part['sha256']:
            raise RuntimeError('Capture changed since it was queued')
          send('PUT', f'{base}/files/{artifact["name"]}/{index}', data=data)
    send('POST', base + '/complete')
  event.update(state='uploaded', uploaded_at=time.time(), upload_error='')  # noqa: TID251 - durable timestamp
  P.atomic_json(str(directory / 'event.json'), event, durable=True)
  # Local report metadata stays, while this derived copy no longer needs space.
  # The owner's saved drive is deliberately not unsaved by upload completion.
  return True
