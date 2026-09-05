#!/usr/bin/env python3
"""Private feedback review using the agent's existing Wrangler authentication."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / 'cloud' / 'feedback'
DB = 'funnypilot-feedback'
BUCKET = 'funnypilot-feedback'


def wrangler(*args):
  local = CLOUD / 'node_modules' / '.bin' / 'wrangler'
  return subprocess.check_output([str(local) if local.exists() else 'wrangler', *args], cwd=CLOUD, text=True)


def sql(query):
  # --file imports a SQL dump and does not return SELECT rows. --command is
  # passed as one argv element, never through a shell or command substitution.
  result = json.loads(wrangler('d1','execute',DB,'--remote','--command',query,'--json'))
  if not result or not all(r.get('success') for r in result):
    raise RuntimeError('Cloud query failed')
  return result[0].get('results', [])


def quote(value):
  return "'" + value.replace("'", "''") + "'"


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest='command', required=True)
  listing = commands.add_parser('list')
  listing.add_argument('--label')
  listing.add_argument('--status', choices=('new','triaged','fixed'))
  listing.add_argument('--limit', type=int, default=20)
  download = commands.add_parser('download')
  download.add_argument('id')
  download.add_argument('--directory', type=Path, required=True)
  review = commands.add_parser('review')
  review.add_argument('id')
  review.add_argument('--status', choices=('new','triaged','fixed'), required=True)
  review.add_argument('--note', required=True)
  review.add_argument('--commit', default='')
  args = parser.parse_args()
  if args.command == 'list':
    conditions = ["upload_status='complete'", "COALESCE(json_extract(manifest,'$.synthetic'),0)=0"]
    if args.label:
      conditions.append('id IN (SELECT event_id FROM event_labels WHERE label=' + quote(args.label) + ')')
    if args.status:
      conditions.append('review_status=' + quote(args.status))
    query = 'SELECT id,route,created_at,commit_sha,labels,review_status,review_note,fix_commit FROM events WHERE '
    print(json.dumps(sql(query + ' AND '.join(conditions) + f' ORDER BY created_at DESC LIMIT {min(100, max(1,args.limit))}'), indent=2))
    return
  if not re.fullmatch('[a-f0-9]{32}', args.id):
    parser.error('invalid event id')
  if args.command == 'review':
    if args.status == 'fixed' and not re.fullmatch('[a-f0-9]{7,40}', args.commit):
      parser.error('fixed requires --commit with the actual fix hash')
    sql('UPDATE events SET review_status=' + quote(args.status) + ',review_note=' + quote(args.note[:4000]) +
        ',fix_commit=' + quote(args.commit) + ' WHERE id=' + quote(args.id) + ';')
    print('Review updated')
    return
  rows = sql('SELECT manifest FROM events WHERE id=' + quote(args.id) + " AND upload_status='complete';")
  if not rows:
    parser.error('no completed event with that id')
  manifest = json.loads(rows[0]['manifest'])
  destination = args.directory / args.id
  destination.mkdir(parents=True, exist_ok=True)
  (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
  for artifact in manifest['artifacts']:
    name = artifact['name']
    if not re.fullmatch('[A-Za-z0-9_.-]{1,100}', name) or name in ('.','..'):
      raise ValueError('unsafe artifact name')
    target = destination / name
    if target.is_symlink():
      raise ValueError('artifact destination is a symlink')
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory() as temp, open(target, 'wb') as out:
      for i, part in enumerate(artifact['parts']):
        path = Path(temp) / 'chunk'
        wrangler('r2','object','get',f'{BUCKET}/events/{args.id}/{name}/{i}','--remote','--file',str(path))
        data = path.read_bytes()  # bounded to the protocol's 4 MiB part size
        if len(data) != part['size'] or hashlib.sha256(data).hexdigest() != part['sha256']:
          raise ValueError('download checksum mismatch')
        digest.update(data)
        out.write(data)
    if target.stat().st_size != artifact['size'] or digest.hexdigest() != artifact['sha256']:
      raise ValueError('artifact checksum mismatch')
  print(destination)


if __name__ == '__main__':
  main()
