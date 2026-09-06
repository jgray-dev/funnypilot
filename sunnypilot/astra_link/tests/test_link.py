import base64
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sunnypilot.astra_link.core import Journal, canonical, digest, identity_equal, load_config, private_read, private_write
from sunnypilot.astra_link.daemon import Device
from sunnypilot.astra_link.execution import Lease, probe_identity, run_process
from sunnypilot.astra_link.files import Files
from sunnypilot.astra_link.state import Safety
from openpilot.tools.astra_link import configure

IDENTITY = {"branch": "test", "commit": "1" * 40, "dirty": False, "version": "3.7.3e"}


class Parked:
  parked = True

  def offroad(self):
    return self.parked

  def snapshot(self):
    return {"mode": "offroad" if self.parked else "onroad", "reason": "test"}


class Server:
  def __init__(self):
    self.calls = []
    self.fail = None
    self.request = None

  def post(self, route, value):
    self.calls.append((route, value))
    if route == self.fail:
      raise OSError("injected failure")
    if route == "grant":
      return {**value, "leaseSeconds": 5}
    if route == "lease":
      return {"leaseSeconds": 5}
    if route == "poll":
      return {"request": self.request, "pollAfter": 15}
    return {"ok": True}


@pytest.fixture
def device(tmp_path):
  server = Server()
  return Device(server, Parked(), Journal(str(tmp_path / "private" / "receipts.json")), identity=lambda: dict(IDENTITY))


def request(device, kind="command", args=None):
  args = args if args is not None else {"argv": ["/bin/true"], "cwd": "/tmp", "timeout": 3}
  return {"id": "request1", "kind": kind, "args": args, "digest": digest(kind, args),
          "generation": device.generation, "bootId": device.boot_id, "identity": dict(IDENTITY),
          "expires": device.wall() * 1000 + 120000, "status": "claimed" if kind == "command" else "dispatched"}


def test_digest_exact_unicode():
  assert canonical({"kind": "identity", "args": {"z": "é", "a": [1, {"b": 2}]}}) == \
    b'{"args":{"a":[1,{"b":2}],"z":"\xc3\xa9"},"kind":"identity"}'


def test_command_production_and_no_replay(device):
  req = request(device)
  assert device.dispatch(req)["ok"]
  with pytest.raises(ValueError):
    device.dispatch(req)
  device.uncertain()
  req.update(generation=device.generation)
  assert not device.dispatch(req)["ok"]
  assert len([c for c in device.transport.calls if c[0] == "grant"]) == 1


@pytest.mark.parametrize("fault", ["corrupt", "full", "write", "symlink", "mode"])
def test_journal_failure_never_grants(device, tmp_path, monkeypatch, fault):
  path = device.journal.path
  private_write(path, {"version": 1, "receipts": {}})
  if fault == "corrupt":
    with open(path, "w") as stream:
      stream.write("not JSON")
  elif fault == "full":
    private_write(path, {"version": 1, "receipts": {str(i): "f" * 64 for i in range(256)}})
  elif fault == "write":
    monkeypatch.setattr(os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
  elif fault == "symlink":
    os.unlink(path)
    os.symlink(tmp_path / "target", path)
  elif fault == "mode":
    os.chmod(path, 0o644)
  assert not device.dispatch(request(device))["ok"]
  assert not device.transport.calls


def test_lost_grant_never_spawn_or_replay(device):
  device.transport.fail = "grant"
  device.runner = lambda *_: pytest.fail("must not execute")
  req = request(device)
  assert not device.dispatch(req)["ok"]
  restarted = Device(device.transport, Parked(), device.journal, identity=lambda: IDENTITY)
  req.update(generation=restarted.generation, bootId=restarted.boot_id)
  assert not restarted.dispatch(req)["ok"]
  assert len(device.transport.calls) == 1


def test_lost_result_rotates_generation_without_reexecution(device):
  device.transport.request = request(device)
  device.step()
  assert device.pending["result"]["ok"]
  device.transport.fail = "result"
  with pytest.raises(OSError):
    device.step()
  old = device.generation
  device.uncertain()
  assert device.generation != old and device.pending is None
  assert len([c for c in device.transport.calls if c[0] == "grant"]) == 1


@pytest.mark.parametrize("change", ["digest", "generation", "bootId", "expires", "status"])
def test_invalid_binding(device, change):
  req = request(device)
  req[change] = 0 if change == "expires" else "wrong"
  with pytest.raises(ValueError):
    device.dispatch(req)
  assert not device.transport.calls


def native(now, pandas=None):
  class SM(dict):
    pass
  sm = SM(pandaStates=pandas if pandas is not None else [
    SimpleNamespace(ignitionLine=False, ignitionCan=False, pandaType="uno")])
  sm.seen = dict.fromkeys(sm, True)
  sm.valid = dict.fromkeys(sm, True)
  sm.recv_time = dict.fromkeys(sm, now)
  return sm


@pytest.mark.parametrize("fault", ["empty", "unknown", "ignition", "stale", "invalid", "unseen", "started"])
def test_safety_fails_closed(fault):
  safety = Safety(clock=lambda: 10)
  sm = native(10)
  if fault == "empty":
    sm["pandaStates"] = []
  elif fault == "unknown":
    sm["pandaStates"][0].pandaType = "unknown"
  elif fault == "ignition":
    sm["pandaStates"][0].ignitionCan = True
  elif fault == "stale":
    sm.recv_time["pandaStates"] = 7
  elif fault == "invalid":
    sm.valid["pandaStates"] = False
  elif fault == "unseen":
    sm.seen["pandaStates"] = False
  safety.update(sm, started=fault == "started")
  assert not safety.offroad()


def test_safety_holds_no_device_state_reader():
  # deviceState is a full msgq service once sunnylink is registered; the
  # monitor takes `started` from manager's IsOnroad param instead.
  source = (Path(__file__).resolve().parents[1] / "state.py").read_text()
  assert '"deviceState"' not in source and 'IsOnroad' in source


def test_safety_expires_by_receive_clock_not_last_monitor_update():
  now = [10.0]
  safety = Safety(clock=lambda: now[0])
  safety.update(native(8.1))
  assert safety.offroad()
  now[0] = 10.2
  assert not safety.offroad()


@pytest.mark.parametrize("special", ["symlink", "fifo", "directory", "parent_link"])
def test_files_deny_symlinks_and_special(tmp_path, special):
  root = str(tmp_path)
  path = tmp_path / "file"
  if special == "symlink":
    path.symlink_to("/etc/passwd")
  elif special == "fifo":
    os.mkfifo(path)
  elif special == "directory":
    path.mkdir()
  else:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "text").write_text("data")
    path.symlink_to(actual, target_is_directory=True)
    path = path / "text"
  with pytest.raises((OSError, ValueError)):
    Files(roots=(root,), recordings=()).raw(str(path), 0, 4096)


@pytest.mark.parametrize("name", ["../outside", ".git/config", ".ssh/key", "cloud.json", "credentials.json", "private.pem", "config.json"])
def test_private_paths_denied(tmp_path, name):
  with pytest.raises(ValueError):
    Files(roots=(str(tmp_path),), recordings=()).locate(str(tmp_path) + "/" + name)


def test_bounded_reads_redaction_and_onroad_gate(tmp_path):
  path = tmp_path / "log"
  path.write_bytes(b"token=abc123\n" + b"a" * 40000)
  files = Files(roots=(str(tmp_path),), recordings=())
  safety = Parked()
  result = files.execute("read_file", {"path": str(path), "length": 4096}, safety)
  assert b"abc123" not in base64.b64decode(result["data"])
  assert result["truncated"] and result["redacted"]
  safety.parked = False
  assert files.execute("tail_log", {"path": str(path), "length": 100}, safety)["offset"] == path.stat().st_size - 100
  with pytest.raises(ValueError):
    files.execute("read_file", {"path": str(path), "length": 32768}, safety)


def test_recording_requires_explicit_offroad(tmp_path):
  path = tmp_path / "rlog"
  path.write_bytes(b"recording")
  files = Files(roots=(), recordings=(str(tmp_path),))
  for args, parked in [({}, True), ({"allowRecording": True}, False)]:
    safety = Parked()
    safety.parked = parked
    with pytest.raises(ValueError):
      files.execute("read_file", {"path": str(path), "length": 1, **args}, safety)


def test_listing_bounded_and_filtered(tmp_path):
  for i in range(220):
    (tmp_path / str(i)).write_text("")
  (tmp_path / ".secret").write_text("secret")
  result = Files(roots=(str(tmp_path),)).execute("list_files", {"path": str(tmp_path)}, Parked())
  assert len(result["entries"]) <= 200 and result["truncated"]
  assert all(not entry["name"].startswith(".") for entry in result["entries"])


def test_output_flood_drains_and_bounds():
  result = run_process([sys.executable, "-c", "import os; [os.write(1,b'x'*8192) for _ in range(512)]"], "/tmp", 3)
  assert result["ok"] and result["truncated"] and len(result["output"]) == 32768


def test_group_timeout_kills_descendant_holding_pipe():
  started = time.monotonic()
  result = run_process([sys.executable, "-c", "import os,time; os.fork(); time.sleep(30)"], "/tmp", 0.2)
  assert not result["ok"] and time.monotonic() - started < 2


def test_safety_cancels_running_process():
  started = time.monotonic()
  result = run_process(["/bin/sleep", "30"], "/tmp", 30, lambda: time.monotonic() - started < 0.15)
  assert not result["ok"] and time.monotonic() - started < 2


def test_lease_loss_cancels_and_carries_fresh_state():
  server = Server()
  server.fail = "lease"
  req = {"id": "id", "identity": IDENTITY}
  lease = Lease(server, req, "generation", Parked(), time.monotonic() + 30, identity=lambda: IDENTITY)
  lease.start()
  result = run_process(["/bin/sleep", "30"], "/tmp", 30, lease.valid)
  lease.stop.set()
  assert not result["ok"]
  assert server.calls[0][1]["identity"] == IDENTITY
  assert server.calls[0][1]["state"]["mode"] == "offroad"


def test_identity_real_git_full_hash_and_untracked(tmp_path):
  subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
  subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                  "commit", "--allow-empty", "-qm", "initial"], check=True)
  clean = probe_identity(str(tmp_path))
  assert len(clean["commit"]) == 40 and clean["dirty"] is False
  (tmp_path / "untracked").write_text("data")
  assert probe_identity(str(tmp_path))["dirty"] is True
  assert probe_identity(str(tmp_path / "absent"))["dirty"] is None


def test_pair_token_persisted_before_exchange_and_reused(tmp_path):
  path = str(tmp_path / "private" / "config.json")
  tokens = []

  class Pair:
    def post(self, route, payload):
      tokens.append(payload["token"])
      assert private_read(path)["token"] == payload["token"]
      if len(tokens) == 1:
        raise OSError("lost acknowledgment")
      return {"deviceId": "device1"}

  with pytest.raises(OSError):
    configure("pair", Parked(), Pair, path, lambda _: "a" * 64)
  configure("pair", Parked(), Pair, path, lambda _: "a" * 64)
  assert tokens[0] == tokens[1]
  assert load_config(path)["enabled"]
  assert os.stat(path).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fault", ["slow_grant", "slow_identity", "changed_identity", "ignition"])
def test_grant_freshness_recheck_prevents_spawn(device, fault):
  now = [10.0]
  device.clock = lambda: now[0]
  original = device.transport.post

  def post(route, value):
    result = original(route, value)
    if route == "grant":
      if fault == "slow_grant":
        now[0] += 2.1
      elif fault == "ignition":
        device.safety.parked = False
      elif fault == "changed_identity":
        device.identity = lambda: {**IDENTITY, "dirty": True}
      else:
        def identity():
          now[0] += 2.1
          return IDENTITY
        device.identity = identity
    return result

  device.transport.post = post
  device.runner = lambda *_: pytest.fail("stale grant must not spawn")
  assert not device.dispatch(request(device))["ok"]


def test_onroad_command_performs_no_journal_io(device):
  device.safety.parked = False
  device.journal.record = lambda _: pytest.fail("onroad journal IO")
  assert not device.dispatch(request(device))["ok"]
  assert device.transport.calls == []


def test_disabled_daemon_does_not_start_native_or_network(monkeypatch):
  from sunnypilot.astra_link import daemon

  class Stop:
    stopped = False

    def is_set(self):
      return self.stopped

    def wait(self, delay):
      assert delay == 30
      self.stopped = True

  monkeypatch.setattr(daemon, "load_config", lambda: {"enabled": False})
  monkeypatch.setattr(daemon, "Safety", lambda: pytest.fail("native initialization while disabled"))
  monkeypatch.setattr(daemon, "Transport", lambda *_: pytest.fail("network initialization while disabled"))
  daemon.main(Stop())


@pytest.mark.parametrize("status,encoding,body,valid", [
  (200, "identity", b'{"ok":true}', True),
  (302, "identity", b'{"ok":true}', False),
  (200, "gzip", b'{"ok":true}', False),
  (200, "identity", b"x" * 65537, False),
  (200, "identity", b"[]", False),
])
def test_transport_fixed_origin_bounds_no_redirects(status, encoding, body, valid):
  from sunnypilot.astra_link.core import Transport

  class Response:
    status_code = status
    headers = {"Content-Encoding": encoding}

    def __enter__(self):
      return self

    def __exit__(self, *_):
      pass

    def iter_content(self, size):
      for index in range(0, len(body), size):
        yield body[index:index + size]

  class Session:
    def post(self, url, **kwargs):
      assert url == "https://astra.jgray.cc/device/poll"
      assert kwargs["allow_redirects"] is False
      assert kwargs["timeout"] == (2, 2)
      assert kwargs["headers"]["Authorization"] == "Bearer " + "a" * 64
      return Response()

  transport = Transport.__new__(Transport)
  transport.session, transport.token = Session(), "a" * 64
  if valid:
    assert transport.post("poll", {}) == {"ok": True}
  else:
    with pytest.raises(ValueError):
      transport.post("poll", {})


def test_private_storage_rejects_parent_symlink(tmp_path):
  actual = tmp_path / "actual"
  actual.mkdir(mode=0o700)
  link = tmp_path / "link"
  link.symlink_to(actual, target_is_directory=True)
  with pytest.raises(OSError):
    private_write(str(link / "config.json"), {"token": "a" * 64})
  assert not list(actual.iterdir())


def test_listing_and_redaction_respect_wire_budget(tmp_path):
  for i in range(200):
    (tmp_path / (str(i) + "x" * 245)).write_text("")
  files = Files(roots=(str(tmp_path),))
  result = files.execute("list_files", {"path": str(tmp_path)}, Parked())
  assert len(canonical(result)) < 65536 and result["truncated"]
  path = tmp_path / "log"
  path.write_bytes(b"token=x " * 4096)
  result = files.execute("read_file", {"path": str(path)}, Parked())
  assert len(base64.b64decode(result["data"])) <= 32768
  assert len(canonical(result)) < 65536


def test_command_control_byte_flood_respects_wire_budget(device):
  req = request(device, args={"argv": [sys.executable, "-c", "import os; os.write(1,b'\\x01'*40000)"], "cwd": "/tmp", "timeout": 3})
  result = device.dispatch(req)
  assert result["ok"] and result["truncated"]
  assert len(canonical(result)) <= 60000


def test_identity_heartbeat_cache_and_fresh_explicit_probe(device):
  calls = []

  def identity():
    value = {**IDENTITY, "dirty": bool(calls)}
    calls.append(value)
    return value

  device.identity = identity
  assert not calls
  device.step()
  device.step()
  assert len(calls) == 1
  result = device.dispatch(request(device, "identity", {}))
  assert result["identity"]["dirty"] is True
  assert result["startupIdentity"]["dirty"] is False
  assert result["runningCode"] == "not established by disk checkout"
  assert len(calls) == 2


def test_default_small_read_allowed_onroad(tmp_path):
  path = tmp_path / "diagnostic"
  path.write_bytes(b"test" * 2000)
  safety = Parked()
  safety.parked = False
  result = Files(roots=(str(tmp_path),)).execute("read_file", {"path": str(path)}, safety)
  assert result["ok"] and result["length"] == 4096


@pytest.mark.parametrize("field", ["token", "deviceId"])
@pytest.mark.parametrize("value", [None, 123, True, {}, [], "", "invalid value"])
def test_malformed_config_values_fail_with_value_error(tmp_path, field, value):
  path = str(tmp_path / "private" / "config.json")
  config = {"enabled": True, "token": "a" * 64, "deviceId": "device1", field: value}
  private_write(path, config)
  with pytest.raises(ValueError):
    load_config(path)


@pytest.mark.parametrize("identity", [
  {}, None, {"branch": None, "commit": None, "dirty": None, "version": None},
  {**IDENTITY, "commit": "a" * 7}, {**IDENTITY, "commit": "x" * 40},
  {**IDENTITY, "commit": 123}, {**IDENTITY, "dirty": None}, {**IDENTITY, "dirty": 0},
  {**IDENTITY, "version": 3}, {key: value for key, value in IDENTITY.items() if key != "branch"},
])
def test_incomplete_identity_never_authorizes_command(device, identity):
  assert not identity_equal(identity, identity)
  assert not identity_equal(IDENTITY, identity)
  assert not identity_equal(identity, IDENTITY)
  if isinstance(identity, dict):
    req = request(device)
    req["identity"] = identity
    device.identity = lambda: identity
    device.journal.record = lambda _: pytest.fail("unknown identity must not write a receipt")
    assert not device.dispatch(req)["ok"]
    assert device.transport.calls == []


def test_known_identity_allows_null_branch_and_version_only():
  identity = {**IDENTITY, "branch": None, "version": None}
  assert identity_equal(identity, {**identity, "observedAt": 123})
  assert not identity_equal(identity, {**identity, "dirty": True})


def test_imports_do_not_load_native_or_requests():
  code = """
import sys
import sunnypilot.astra_link.daemon
import tools.astra_link
assert 'requests' not in sys.modules
assert 'cereal.messaging' not in sys.modules
"""
  subprocess.run([sys.executable, "-c", code], check=True)
