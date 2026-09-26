"""Retain incident segments independently of the drive's explicit saved flag.

Closed files are hard-linked on the same /data filesystem: bounded metadata IO,
no video transcode or duplicate large copy. Retention and web deletion share the
lock. Pins survive restart and are removed only after durable clip metadata.
"""
import os
from pathlib import Path

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.navd import drive_index as D
from openpilot.system.loggerd import drive_retention as R

MAX_FILE = 128 * 1024**2
MAX_EVENT = 512 * 1024**2


def prepare_clip(directory, log_root=D.REALDATA_ROOT):
  directory = Path(directory)
  event = P.read_json(str(directory / 'event.json'), {})
  if event.get('state') != 'queued' or not event.get('clip_only'):
    return False
  with R.locked_saved(log_root):
    pins = R.read_clip_pins(log_root)
    if (directory / 'clip.json').exists():
      clip = P.read_json(str(directory / 'clip.json'))
      if (not isinstance(clip, dict) or not isinstance(clip.get('files'), list) or
          any(not isinstance(name, str) or Path(name).name != name or not name.startswith('segment-') or
              not (directory / name).is_file() or (directory / name).is_symlink() for name in clip['files'])):
        raise ValueError('invalid or incomplete incident clip')
      # Interrupted after metadata fsync but before releasing protection.
      if event['id'] in pins:
        pins.pop(event['id'])
        R.write_clip_pins(log_root, pins)
      return True
    n = event['segment_at_report']
    segments = [(s, D.segment_path(event['route'], s, log_root)) for s in range(max(0, n-1), n+2)]
    if any(path and Path(path).is_dir() and R.is_locked(path) for _, path in segments):
      return False
    files, missing = [], []
    total = sum(p.stat().st_size for p in directory.glob('*.jsonl'))
    for segment, path in segments:
      if path is None or not Path(path).is_dir():
        missing.append(f'segment {segment}')
        continue
      for name in ('qlog.zst', 'qcamera.ts', 'rlog.zst'):
        source = Path(path) / name
        target = directory / f'segment-{segment}-{name}'
        if not source.is_file() or source.is_symlink():
          missing.append(f'segment {segment}: {name}')
          continue
        size = source.stat().st_size
        if size > MAX_FILE or total+size > MAX_EVENT:
          missing.append(target.name+' (size limit)')
          continue
        # link() is atomic. Retry only accepts the same inode, never a stale or
        # substituted file. EXDEV/ENOSPC leave the pins in place and retry later.
        try:
          os.link(source, target, follow_symlinks=False)
        except FileExistsError:
          if target.is_symlink() or not os.path.samefile(source, target):
            raise ValueError('incident clip file conflict') from None
        total += size
        files.append(target.name)
    P.atomic_json(str(directory / 'clip.json'), {'files': files, 'missing': missing}, durable=True)
    pins.pop(event['id'], None)
    R.write_clip_pins(log_root, pins)
  return True


def release_uploaded_clip(directory):
  """Cloud-acknowledged road artifacts no longer need local disk space.

  Keep report metadata and diagnostic telemetry; never remove a queued file or
  an original route. Unlinking our hardlink cannot change an owner's saved drive.
  """
  directory = Path(directory)
  event = P.read_json(str(directory / 'event.json'), {})
  if event.get('state') != 'uploaded' or not event.get('clip_only'):
    return
  for path in directory.glob('segment-*'):
    if path.is_file() and not path.is_symlink():
      try:
        path.unlink()
      except OSError:
        # Cloud acknowledgement remains authoritative. Retry local reclamation
        # later; never turn an already uploaded report back into a queued one.
        continue
