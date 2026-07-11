"""FunnyPilot v3.3.1 — verified radar-tracks enable flow tests.

The enable must claim success ONLY when the post-write read-back shows the
tracks bit, and every step must land in the JSONL evidence log.
"""
import json

import opendbc.sunnypilot.car.hyundai.enable_radar_tracks as ert


class FakeQuery:
  """Scripted IsoTpParallelQuery: responses served in construction order."""
  script: list = []  # each entry: dict of {addr: payload_bytes} returned by get_data
  calls: list = []

  def __init__(self, sendcan, logcan, bus, addrs, requests, responses):
    FakeQuery.calls.append(bytes(requests[0]))
    self._resp = FakeQuery.script.pop(0) if FakeQuery.script else {}

  def get_data(self, timeout):
    return self._resp


def run_enable(monkeypatch, tmp_path, script):
  monkeypatch.setattr(ert, "IsoTpParallelQuery", FakeQuery)
  monkeypatch.setattr(ert, "TRIAGE_DIR", str(tmp_path))
  FakeQuery.script = list(script)
  FakeQuery.calls = []
  result = ert.enable_radar_tracks(None, None, bus=0, addr=0x7d0, timeout=0.01, retry=2)
  log_path = tmp_path / ert.LOG_NAME
  records = []
  if log_path.exists():
    with open(log_path) as f:
      records = [json.loads(line) for line in f if line.strip()]
  return result, records


SESSION_OK = {0x7d0: b"\x50\x07"}
IDENT = {0x7d0: b"\x62\xf1\x81DL3RADAR"}
CONFIG_OFF = {0x7d0: b"\x62\x01\x42" + ert.DEFAULT_CONFIG}
WRITE_ACK = {0x7d0: b"\x6e\x01\x42"}
VERIFY_ON = {0x7d0: b"\x62\x01\x42" + ert.TRACKS_ENABLED_CONFIG}
VERIFY_STILL_OFF = {0x7d0: b"\x62\x01\x42" + ert.DEFAULT_CONFIG}
NOTHING: dict = {}


class TestEnableRadarTracks:
  def test_verified_success(self, monkeypatch, tmp_path):
    # session, 3 ident reads, config read, write, verify -> enabled
    ok, recs = run_enable(monkeypatch, tmp_path,
                          [SESSION_OK, IDENT, IDENT, IDENT, CONFIG_OFF, WRITE_ACK, VERIFY_ON])
    assert ok is True
    att = recs[0]
    assert att["session"] is True
    assert att["ident"]["app_sw"] is not None  # device fingerprint captured
    assert att["config"] == ert.DEFAULT_CONFIG.hex()
    assert att["write_ack"] is True
    assert att["verify"] == ert.TRACKS_ENABLED_CONFIG.hex()
    assert att["enabled"] is True

  def test_nacked_write_is_a_failure(self, monkeypatch, tmp_path):
    # upstream would have reported success here; we must not
    ok, recs = run_enable(monkeypatch, tmp_path,
                          [SESSION_OK, IDENT, IDENT, IDENT, CONFIG_OFF, NOTHING, VERIFY_STILL_OFF,   # try 1
                           SESSION_OK, CONFIG_OFF, NOTHING, VERIFY_STILL_OFF])                        # try 2
    assert ok is False
    attempts = [r for r in recs if r.get("kind") == "enable_attempt"]
    assert len(attempts) == 2
    assert attempts[0]["write_ack"] is False
    assert attempts[0]["verify"] == ert.DEFAULT_CONFIG.hex()
    assert attempts[0]["enabled"] is False
    assert recs[-1] == {**recs[-1], "kind": "enable_result", "enabled": False}

  def test_silent_radar_logged(self, monkeypatch, tmp_path):
    # no session response at all: every attempt logged, result False
    ok, recs = run_enable(monkeypatch, tmp_path, [NOTHING, NOTHING])
    assert ok is False
    attempts = [r for r in recs if r.get("kind") == "enable_attempt"]
    assert len(attempts) == 2
    assert all(a["session"] is False for a in attempts)

  def test_already_enabled_short_circuits(self, monkeypatch, tmp_path):
    ok, recs = run_enable(monkeypatch, tmp_path,
                          [SESSION_OK, IDENT, IDENT, IDENT, VERIFY_ON])
    assert ok is True
    assert recs[0]["action"] == "already_enabled"

  def test_log_write_failure_never_raises(self, monkeypatch, tmp_path):
    monkeypatch.setattr(ert, "TRIAGE_DIR", "/proc/definitely/not/writable")
    monkeypatch.setattr(ert, "IsoTpParallelQuery", FakeQuery)
    FakeQuery.script = [SESSION_OK, IDENT, IDENT, IDENT, CONFIG_OFF, WRITE_ACK, VERIFY_ON]
    assert ert.enable_radar_tracks(None, None, timeout=0.01, retry=1) is True
