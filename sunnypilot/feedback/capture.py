"""Durable incident capture. Runs outside UI/plannerd; no network here."""
from collections import deque
from contextlib import contextmanager
import json
import os
import math
from pathlib import Path
import re
import shutil
import time

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.feedback.corner_feedback import MAX_RULES, matches
from openpilot.sunnypilot.navd import drive_index

PRE_SECONDS = 20
POST_SECONDS = 20
MAX_PENDING = 200


def valid_command(cmd, now):
  return (isinstance(cmd, dict) and isinstance(cmd.get('id'), str) and re.fullmatch('[a-f0-9]{32}', cmd['id']) and
          isinstance(cmd.get('labels'), list) and len(cmd['labels']) <= len(P.LABELS) and
          all(isinstance(l, str) and l in P.LABELS for l in cmd['labels']) and
          isinstance(cmd.get('started'), (int, float)) and math.isfinite(cmd['started']) and 0 <= now - cmd['started'] <= 20 and
          isinstance(cmd.get('sent'), (int, float)) and math.isfinite(cmd['sent']) and 0 <= now - cmd['sent'] <= 3)


@contextmanager
def _artifact(path):
  # Never truncate a collected artifact while retrying a failed finalization.
  tmp = path.with_name(path.name + '.tmp')
  with open(tmp, 'w') as out:
    yield out
    out.flush()
    os.fsync(out.fileno())
  os.replace(tmp, path)
  # The durable event.json write fsyncs this same directory before reporting
  # success. Keep the published snapshot on metadata/fsync retries, even when
  # the diagnostic source has rotated or the telemetry ring has aged out.


class Capture:
  def __init__(self, root=P.ROOT, log_root=drive_index.REALDATA_ROOT, identity=None):
    self.root, self.log_root = Path(root), log_root
    self.events = self.root / 'events'
    self.events.mkdir(parents=True, exist_ok=True)
    self.ring = deque(maxlen=4500)  # 45 seconds at 100 Hz; scalars only
    self.active = {}
    self._initializing = {}
    self._prerolled = set()
    self._triaged = set()
    self.identity = identity or {}
    self.rules = P.read_json(str(self.root / 'corners.json'), {'rules': []})
    if not isinstance(self.rules, dict) or not isinstance(self.rules.get('rules'), list):
      self.rules = {'rules': []}
    P.atomic_json(P.CORNER_RULES, self.rules)
    # A restart still leaves useful pre-event data and the protected route.
    for path in self.events.glob('*/event.json'):
      event = P.read_json(str(path))
      if event and event.get('state') == 'capturing':
        event.update(state='queued', capture_interrupted=True)
        P.atomic_json(str(path), event, durable=True)

  def sample(self, row):
    # Reject damaged samples before they can poison every later pre-event dump.
    encoded = json.dumps(row, allow_nan=False, separators=(',', ':')) + '\n'
    self.ring.append(row)
    affected = [event for event in self.active.values() if row['t'] <= event['mono_time'] + POST_SECONDS]
    for event in affected:
      if event['id'] in self._initializing and event['id'] not in self._prerolled:
        # The failed pre-roll will be retried from its retained snapshot. Do
        # not append to a file that retry must replace and pretend it survived.
        event['capture_interrupted'] = True
        continue
      try:
        with open(self.events / event['id'] / 'telemetry.jsonl', 'a') as f:
          f.write(encoded)
      except OSError:
        # The caller backs off for all events without losing this Capture
        # object; finalization persists the partial-data flag for that gap.
        for interrupted in affected:
          interrupted['capture_interrupted'] = True
        raise

  def report(self, cmd, route, now=None):
    now = time.monotonic() if now is None else now
    if not valid_command(cmd, now):
      raise ValueError('invalid or stale feedback command')
    event_id = cmd['id']
    event = self.active.get(event_id)
    if event is None:
      if len(self.active) >= 3:
        raise ValueError('Already capturing three reports; try again shortly')
      existing = self.events / event_id / 'event.json'
      if existing.exists():
        raise ValueError('report already closed')
      entries = list(self.events.glob('*/event.json'))
      if len(entries) >= MAX_PENDING:
        uploaded = [(p, P.read_json(str(p), {})) for p in entries]
        uploaded = sorted(((p, e) for p, e in uploaded if e.get('state') == 'uploaded'), key=lambda pe: pe[1].get('created_at', 0))
        for p, _ in uploaded[:max(0, len(entries) - MAX_PENDING + 1)]:
          shutil.rmtree(p.parent)
      if sum(1 for p in self.events.iterdir() if p.is_dir()) >= MAX_PENDING:
        raise ValueError('Feedback storage is full; upload queued reports first')
      # Same hard save as the web UI: full route, including later segments.
      drive_index.set_route_saved(route, True, self.log_root)
      context = P.read_json(P.CORNER_CONTEXT, {})
      if not isinstance(context, dict) or not isinstance(context.get('t'), (float, int)) or not 0 <= now - context['t'] < 1:
        context = {}
      segs = drive_index.route_segments(route, self.log_root)
      event = dict(self.identity, id=event_id, route=route, created_at=time.time(), mono_time=cmd['started'],  # noqa: TID251 - cross-reboot event index
                   labels=[], state='capturing', corner=context, remedy='No map correction requested',
                   segment_at_report=max(segs) if segs else 0, pre_seconds=PRE_SECONDS, post_seconds=POST_SECONDS)
      # Retain the original corner and request identity even if mkdir, the
      # pre-roll, or initial metadata persistence fails. This is not an ack.
      self.active[event_id] = event
      self._initializing[event_id] = [row for row in self.ring if cmd['started'] - PRE_SECONDS <= row['t'] <= now]
    if event_id in self._initializing and event_id not in self._prerolled:
      directory = self.events / event_id
      directory.mkdir(exist_ok=True)
      with _artifact(directory / 'telemetry.jsonl') as f:
        for row in self._initializing[event_id]:
          f.write(json.dumps(row, allow_nan=False, separators=(',', ':')) + '\n')
      self._prerolled.add(event_id)
    previous = set(event['labels'])
    event = dict(event, labels=sorted(previous | set(cmd['labels'])))
    if 'unnecessary_slowdown' in event['labels'] and 'unnecessary_slowdown' not in previous:
      c = event['corner']
      if (c.get('governing') and not c.get('unmanageable') and
          all(isinstance(c.get(k), (int, float)) and math.isfinite(c[k]) for k in ('lat', 'lon', 'bearing'))):
        rules = [r for r in self.rules['rules'] if not matches(r, c['lat'], c['lon'], c['bearing'])]
        rules.append({k:c[k] for k in ('lat','lon','bearing')} | {'event_id':event_id})
        candidate_rules = dict(self.rules, rules=rules[-MAX_RULES:])
        # Durable first, then publish. Only successful writes advance memory;
        # the uncommitted label retries both writes, including publication.
        P.atomic_json(str(self.root / 'corners.json'), candidate_rules, durable=True)
        P.atomic_json(P.CORNER_RULES, candidate_rules)
        self.rules = candidate_rules
        event['remedy'] = 'Map corner relief saved (bounded; other limits still apply)'
      else:
        event['remedy'] = 'Report saved; no eligible governing map corner'
    P.atomic_json(str(self.events / event_id / 'event.json'), event, durable=True)
    self.active[event_id] = event
    self._initializing.pop(event_id, None)
    self._prerolled.discard(event_id)
    message = event['remedy'] if 'unnecessary_slowdown' in event['labels'] else 'Drive saved. Capturing surrounding data.'
    P.atomic_json(P.STATUS, {'id':event_id, 'labels':event['labels'], 'saved':True, 'message':message, 't':now})
    return event

  def finish(self, now=None):
    now = time.monotonic() if now is None else now
    for event_id, event in list(self.active.items()):
      if now >= event['mono_time'] + POST_SECONDS and event_id not in self._initializing:
        if event_id not in self._triaged:
          self._triage(event)
          self._triaged.add(event_id)
        candidate = dict(event, state='queued')
        P.atomic_json(str(self.events / event_id / 'event.json'), candidate, durable=True)
        del self.active[event_id]
        self._triaged.discard(event_id)

  def _triage(self, event):
    # Capture motion-credit/override/EPS diagnostics before their rotation.
    # Each source is capped at 4 MiB; bounded line reads avoid damaged log lines.
    start = event['created_at'] - PRE_SECONDS
    end = event['created_at'] + POST_SECONDS
    with _artifact(self.events / event['id'] / 'triage.jsonl') as out:
      for name in ('lat_interp.jsonl.1', 'lat_interp.jsonl'):
        try:
          source = open('/data/funnypilot_triage/' + name)
        except FileNotFoundError:
          continue
        with source:
          remaining = 4 * 1024 * 1024
          while remaining > 0:
            line = source.readline(min(65536, remaining))
            if not line:
              break
            remaining -= len(line)
            try:
              row = json.loads(line)
              selected = start <= row.get('t', 0) <= end
            except (ValueError, TypeError, AttributeError):
              continue
            if selected:
              out.write(line)
