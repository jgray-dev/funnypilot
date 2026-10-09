#!/usr/bin/env python3
"""Inspect and fetch harvested drives using the agent's Wrangler authentication.

Fine for stats, single drives and spot checks. For bulk training sync use an R2
S3 token with rclone (docs/harvest-3.7.13.md): wrangler fetches one object per call.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / 'cloud' / 'drives'
DB = 'funnypilot-drives'
BUCKET = 'funnypilot-drives'
ROUTE = re.compile(r'[A-Za-z0-9|_-]{1,128}')
ARTIFACT = re.compile(r'(fcamera\.hevc|ecamera\.hevc|rlog\.zst|qlog\.zst|qcamera\.ts)')

STATS = """SELECT COUNT(*) AS segments, COUNT(DISTINCT route) AS routes, ROUND(SUM(bytes)/1073741824.0,2) AS gib,
  datetime(MIN(created_at),'unixepoch') AS first, datetime(MAX(created_at),'unixepoch') AS last
  FROM segments WHERE upload_status='complete'"""
LIST = """SELECT route, COUNT(*) AS segments, ROUND(SUM(bytes)/1048576.0,1) AS mib, MIN(commit_sha) AS commit_sha,
  datetime(MIN(created_at),'unixepoch') AS started FROM segments WHERE upload_status='complete'
  GROUP BY route ORDER BY MIN(created_at) DESC LIMIT {limit}"""


def wrangler(*args):
  local = CLOUD / 'node_modules' / '.bin' / 'wrangler'
  return subprocess.check_output([str(local) if local.exists() else 'wrangler', *args], cwd=CLOUD, text=True)


def sql(query):
  result = json.loads(wrangler('d1', 'execute', DB, '--remote', '--command', query, '--json'))
  if not result or not all(r.get('success') for r in result):
    raise RuntimeError('Cloud query failed')
  return result[0].get('results', [])


def quote(value):
  return "'" + value.replace("'", "''") + "'"


def fetch_artifact(route, segment, artifact, target):
  if target.is_symlink():
    raise ValueError('artifact destination is a symlink')
  if target.is_file() and target.stat().st_size == artifact['size']:
    existing = hashlib.sha256()
    with target.open('rb') as stream:
      while chunk := stream.read(4 * 1024 * 1024):
        existing.update(chunk)
    if existing.hexdigest() == artifact['sha256']:
      return
  digest = hashlib.sha256()
  with tempfile.TemporaryDirectory(dir=target.parent) as temp:
    staged = Path(temp) / 'artifact'
    with staged.open('wb') as out:
      for i, part in enumerate(artifact['parts']):
        chunk = Path(temp) / 'chunk'
        wrangler('r2', 'object', 'get', f"{BUCKET}/drives/{route}/{segment}/{artifact['name']}/{i}", '--remote', '--file', str(chunk))
        data = chunk.read_bytes()  # bounded to the protocol's 16 MiB part size
        if len(data) != part['size'] or hashlib.sha256(data).hexdigest() != part['sha256']:
          raise ValueError('download checksum mismatch')
        digest.update(data)
        out.write(data)
    if staged.stat().st_size != artifact['size'] or digest.hexdigest() != artifact['sha256']:
      raise ValueError('artifact checksum mismatch')
    staged.replace(target)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest='command', required=True)
  commands.add_parser('stats')
  listing = commands.add_parser('list')
  listing.add_argument('--limit', type=int, default=30)
  download = commands.add_parser('download')
  download.add_argument('route')
  download.add_argument('--directory', type=Path, required=True)
  download.add_argument('--files', default='rlog.zst,fcamera.hevc,ecamera.hevc,qlog.zst,qcamera.ts')
  args = parser.parse_args()
  if args.command == 'stats':
    print(json.dumps(sql(STATS), indent=2))
  elif args.command == 'list':
    print(json.dumps(sql(LIST.format(limit=min(200, max(1, args.limit)))), indent=2))
  else:
    if not ROUTE.fullmatch(args.route):
      parser.error('invalid route')
    wanted = set(args.files.split(','))
    if not all(ARTIFACT.fullmatch(f) for f in wanted):
      parser.error('unknown artifact name')
    rows = sql('SELECT segment, manifest FROM segments WHERE route=' + quote(args.route) + " AND upload_status='complete' ORDER BY segment;")
    if not rows:
      parser.error('no completed segments for that route')
    for row in rows:
      manifest = json.loads(row['manifest'])
      destination = args.directory / args.route / str(row['segment'])
      destination.mkdir(parents=True, exist_ok=True, mode=0o700)
      (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
      for artifact in manifest['artifacts']:
        if artifact['name'] in wanted:
          fetch_artifact(args.route, row['segment'], artifact, destination / artifact['name'])
    print(args.directory / args.route)


if __name__ == '__main__':
  main()
