import hashlib
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from openpilot.sunnypilot.feedback import protocol as P, uploader as U
from openpilot.sunnypilot.feedback.clips import release_uploaded_clip


@pytest.fixture
def event(tmp_path):
  event_id = 'a'*32
  directory = tmp_path / event_id
  directory.mkdir()
  (directory / 'telemetry.jsonl').write_text('{"t":1}\n')
  (directory / 'segment-0-qcamera.ts').write_bytes(b'road video')
  P.atomic_json(str(directory / 'event.json'), dict(id=event_id, route='test', state='queued', segment_at_report=0, clip_only=True))
  P.atomic_json(str(directory / 'clip.json'), {'files':['segment-0-qcamera.ts'], 'missing':['segment 1']})
  return directory


def session(monkeypatch, fail=False):
  calls=[]
  class Session:
    def __enter__(self):
      self.headers={}
      return self
    def __exit__(self,*args):pass
    def request(self, method, url, **kwargs):
      calls.append((method,url,kwargs))
      if fail and url.endswith('/complete'):
        raise OSError('lost completion reply')
      return NS(raise_for_status=lambda:None,json=lambda:{'ok':True})
  monkeypatch.setattr(U.requests,'Session',Session)
  return calls


def test_upload_survives_missing_original_and_releases_only_after_ack(event, monkeypatch):
  calls=session(monkeypatch)
  release_uploaded_clip(event)
  assert (event/'segment-0-qcamera.ts').exists()
  assert U.upload_event(event, dict(endpoint='https://example.invalid',token='test'), log_root='/nonexistent')
  manifest = calls[0][2]['json']
  assert manifest['missing']==['segment 1']
  assert len(manifest['artifacts'])==2
  for a in manifest['artifacts']:
    sent=next(c[2]['data'] for c in calls if '/files/'+a['name']+'/' in c[1])
    assert hashlib.sha256(sent).hexdigest()==a['sha256']
  assert P.read_json(str(event/'event.json'))['state']=='uploaded'
  assert not (event/'segment-0-qcamera.ts').exists()
  assert (event/'telemetry.jsonl').exists()


def test_lost_ack_preserves_clip_and_retries_identical_manifest(event, monkeypatch):
  first=session(monkeypatch,True)
  with pytest.raises(OSError):
    U.upload_event(event, dict(endpoint='https://example.invalid',token='test'))
  assert (event/'segment-0-qcamera.ts').exists()
  assert P.read_json(str(event/'event.json'))['state']=='queued'
  second=session(monkeypatch)
  assert U.upload_event(event, dict(endpoint='https://example.invalid',token='test'))
  assert first[0][2]['json']==second[0][2]['json']


def test_no_upload_when_gate_closed(event, monkeypatch):
  calls=session(monkeypatch)
  assert not U.upload_event(event, dict(endpoint='https://example.invalid',token='test'), allowed=lambda:False)
  assert not calls and (event/'segment-0-qcamera.ts').exists()


def test_local_unlink_failure_cannot_requeue_acknowledged_report(event, monkeypatch):
  session(monkeypatch)
  original=Path.unlink
  def fail(path,*args,**kw):
    if path.name.startswith('segment-'):
      raise OSError('injected unlink failure')
    return original(path,*args,**kw)
  with monkeypatch.context() as fault:
    fault.setattr(Path,'unlink',fail)
    assert U.upload_event(event, dict(endpoint='https://example.invalid',token='test'))
  assert P.read_json(str(event/'event.json'))['state']=='uploaded'
  release_uploaded_clip(event)
  assert not (event/'segment-0-qcamera.ts').exists()
