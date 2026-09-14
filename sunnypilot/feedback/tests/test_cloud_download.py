import hashlib
import json
import sys
from pathlib import Path

import pytest
from openpilot.tools import feedback_cloud as cloud


def artifact(data):
  digest = hashlib.sha256(data).hexdigest()
  return {'name': 'telemetry.jsonl', 'size': len(data), 'sha256': digest,
          'parts': [{'size': len(data), 'sha256': digest}]}


def prepare(monkeypatch, tmp_path, data):
  ident = 'a' * 32
  monkeypatch.setattr(sys, 'argv', ['feedback_cloud.py', 'download', ident, '--directory', str(tmp_path)])
  monkeypatch.setattr(cloud, 'sql', lambda _: [{'manifest': json.dumps({'artifacts': [artifact(data)]})}])
  directory = tmp_path / ident
  directory.mkdir()
  return directory / 'telemetry.jsonl'


def test_verified_existing_artifact_is_not_downloaded_again(monkeypatch, tmp_path):
  target = prepare(monkeypatch, tmp_path, b'good')
  target.write_bytes(b'good')
  monkeypatch.setattr(cloud, 'wrangler', lambda *args: pytest.fail('verified artifact downloaded again'))
  cloud.main()
  assert target.read_bytes() == b'good'


def test_corrupt_download_does_not_publish_partial_artifact(monkeypatch, tmp_path):
  target = prepare(monkeypatch, tmp_path, b'good')
  target.write_bytes(b'previous')
  def download(*args):
    Path(args[-1]).write_bytes(b'bad')
  monkeypatch.setattr(cloud, 'wrangler', download)
  with pytest.raises(ValueError, match='checksum'):
    cloud.main()
  assert target.read_bytes() == b'previous'
