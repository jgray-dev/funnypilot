"""FunnyPilot v3.2.7 — triage flight-recorder tests (import-light, stdlib only)."""
import json
import os

from openpilot.selfdrive.controls.lib.triage_recorder import TriageRecorder, LatInterpMonitor


def read_jsonl(path):
  with open(path) as f:
    return [json.loads(line) for line in f if line.strip()]


class TestTriageRecorder:
  def test_writes_jsonl_with_timestamp(self, tmp_path):
    r = TriageRecorder("test", directory=str(tmp_path))
    assert r.write({"a": 1})
    recs = read_jsonl(tmp_path / "test.jsonl")
    assert recs[0]["a"] == 1
    assert "t" in recs[0]

  def test_rotation(self, tmp_path):
    r = TriageRecorder("test", directory=str(tmp_path), max_bytes=200)
    for i in range(50):
      assert r.write({"i": i, "pad": "x" * 20})
    assert (tmp_path / "test.jsonl").exists()
    assert (tmp_path / "test.jsonl.1").exists()
    assert os.path.getsize(tmp_path / "test.jsonl") < 200 + 64
    # newest record is in the live file
    assert read_jsonl(tmp_path / "test.jsonl")[-1]["i"] == 49

  def test_never_throws_on_bad_directory(self):
    r = TriageRecorder("test", directory="/proc/definitely/not/writable")
    assert r.write({"a": 1}) is False  # returns False, no exception


class TestLatInterpMonitor:
  def _sample(self, mon, t, health=5.0, ctx=None, **kw):
    defaults = dict(lat_active=True, long_active=True, v_ego=20.0, health_frames=health,
                    lane_change=False, curvature_limited=False, a_target=0.1, accel=0.12)
    defaults.update(kw)
    mon.sample(t, context_fn=ctx, **defaults)

  def test_one_record_per_second(self, tmp_path):
    mon = LatInterpMonitor(TriageRecorder("lat", directory=str(tmp_path)))
    for i in range(250):  # 2.5 s at 100 Hz
      self._sample(mon, i * 0.01)
    recs = read_jsonl(tmp_path / "lat.jsonl")
    assert len(recs) == 2
    assert 95 <= recs[0]["n"] <= 105

  def test_aggregation_values(self, tmp_path):
    mon = LatInterpMonitor(TriageRecorder("lat", directory=str(tmp_path)))
    for i in range(105):
      # degrade health for a few frames mid-second
      h = 2.0 if 50 <= i < 55 else 5.0
      self._sample(mon, i * 0.01, health=h, lat_active=(i % 2 == 0))
    recs = read_jsonl(tmp_path / "lat.jsonl")
    assert len(recs) == 1
    r = recs[0]
    assert r["hmin"] == 2.0          # the degradation is not averaged away
    assert 4.5 < r["havg"] <= 5.0
    assert 0.45 <= r["la"] <= 0.55
    assert r["v"] == 20.0
    assert r["at"] == 0.1 and r["ac"] == 0.12

  def test_context_on_first_and_every_tenth_record(self, tmp_path):
    mon = LatInterpMonitor(TriageRecorder("lat", directory=str(tmp_path)))
    def ctx():
      return {"laf": 2.75}
    for i in range(1150):  # ~11 seconds
      self._sample(mon, i * 0.01, ctx=ctx)
    recs = read_jsonl(tmp_path / "lat.jsonl")
    assert len(recs) >= 11
    assert recs[0].get("ctx") == {"laf": 2.75}
    assert recs[10].get("ctx") == {"laf": 2.75}
    assert all("ctx" not in r for r in recs[1:10])

  def test_context_fn_exception_contained(self, tmp_path):
    mon = LatInterpMonitor(TriageRecorder("lat", directory=str(tmp_path)))
    def boom():
      raise RuntimeError("no")
    for i in range(105):
      self._sample(mon, i * 0.01, ctx=boom)
    recs = read_jsonl(tmp_path / "lat.jsonl")
    assert recs[0]["ctx"] is None  # captured, not raised


class TestWebserverHelpers:
  def test_log_name_whitelist(self):
    from openpilot.sunnypilot.navd.nav_webserver import _TRIAGE_NAME_RE
    assert _TRIAGE_NAME_RE.match("lat_interp.jsonl")
    assert _TRIAGE_NAME_RE.match("code_identity.jsonl.1")
    assert _TRIAGE_NAME_RE.match("marks.jsonl")
    assert not _TRIAGE_NAME_RE.match("../../etc/passwd")
    assert not _TRIAGE_NAME_RE.match("foo/bar.jsonl")
    assert not _TRIAGE_NAME_RE.match("evil.sh")

  def test_append_jsonl_rotation(self, tmp_path, monkeypatch):
    import openpilot.sunnypilot.navd.nav_webserver as ws
    monkeypatch.setattr(ws, "TRIAGE_DIR", str(tmp_path))
    for i in range(100):
      ws._append_jsonl("pulse.jsonl", {"i": i, "pad": "x" * 30}, max_bytes=500)
    assert (tmp_path / "pulse.jsonl").exists()
    assert (tmp_path / "pulse.jsonl.1").exists()

  def test_file_hash(self, tmp_path):
    from openpilot.sunnypilot.navd.nav_webserver import _file_hash
    p = tmp_path / "f.py"
    p.write_text("hello")
    h1 = _file_hash(str(p))
    assert len(h1) == 12
    p.write_text("changed")
    assert _file_hash(str(p)) != h1
    assert _file_hash(str(tmp_path / "missing.py")) == "(missing)"
