"""Optional Astra HTTPS poller. Nothing starts until main is called."""

import math
import os
import random
import re
import threading
import time
import uuid

from sunnypilot.astra_link.core import Journal, Transport, canonical, digest, identity_equal, load_config
from sunnypilot.astra_link.execution import Lease, probe_identity, run_process
from sunnypilot.astra_link.files import Files, redact
from sunnypilot.astra_link.state import Safety


class Device:
  def __init__(self, transport, safety, journal=None, files=None, identity=probe_identity, runner=run_process,
               clock=time.monotonic, wall=time.time, boot_id=None):  # noqa: TID251 - server expiry is Unix milliseconds
    self.transport, self.safety = transport, safety
    self.journal, self.files = journal or Journal(), files or Files()
    self.identity, self.runner, self.clock, self.wall = identity, runner, clock, wall
    self.boot_id = boot_id or str(uuid.uuid4())
    self.generation = str(uuid.uuid4())
    self.seen = set()
    self.pending = None
    self.startup_identity = None
    self.heartbeat_identity = None
    self.identity_observed = float("-inf")

  def observe_identity(self, cached=False):
    if not cached or self.heartbeat_identity is None or self.clock() - self.identity_observed >= 15:
      observed = self.identity()
      if self.startup_identity is None and observed.get("commit") is not None:
        self.startup_identity = dict(observed)
      self.heartbeat_identity = dict(observed)
      self.identity_observed = self.clock()
    return dict(self.heartbeat_identity)

  def validate(self, request):
    if not isinstance(request, dict) or len(canonical(request)) > 65536:
      raise ValueError("invalid request")
    if not isinstance(request.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request["id"]):
      raise ValueError("invalid request id")
    if request.get("generation") != self.generation or request.get("bootId") != self.boot_id:
      raise ValueError("request binding mismatch")
    expires = request.get("expires")
    # Contract expiry is Unix milliseconds, shared with the browser/server.
    if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= self.wall() * 1000:
      raise ValueError("request expired")
    kind, args = request.get("kind"), request.get("args")
    allowed = {"identity": set(), "list_files": {"path", "limit", "offset"},
               "read_file": {"path", "offset", "length", "allowRecording"},
               "tail_log": {"path", "length", "allowRecording"}, "command": {"argv", "cwd", "timeout"}}
    if kind not in allowed or not isinstance(args, dict) or set(args) - allowed[kind]:
      raise ValueError("invalid request arguments")
    if request.get("digest") != digest(kind, args):
      raise ValueError("digest mismatch")
    if request.get("status") != ("claimed" if kind == "command" else "dispatched"):
      raise ValueError("invalid dispatch status")
    if not isinstance(request.get("identity"), dict):
      raise ValueError("missing approval identity")
    if kind == "command":
      argv = args.get("argv")
      if (not isinstance(argv, list) or not 1 <= len(argv) <= 64 or
          any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 4096 for arg in argv) or
          not argv[0].startswith("/") or os.path.basename(argv[0]) in ("sudo", "doas", "su")):
        raise ValueError("invalid command argv")
      cwd = args.get("cwd")
      if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd or len(cwd) > 1024:
        raise ValueError("invalid command cwd")
      if type(args.get("timeout")) is not int or not 1 <= args["timeout"] <= 30:
        raise ValueError("invalid command timeout")

  def command(self, request):
    if not self.safety.can_modify() or not identity_equal(request["identity"], self.identity()):
      raise ValueError("vehicle moving, engaged, state unknown, or approval identity mismatch")
    if not self.safety.can_modify():
      raise ValueError("stationary/disengaged permission expired")
    self.journal.record(request)
    if not self.safety.can_modify():
      raise ValueError("stationary/disengaged permission expired")
    started = self.clock()
    grant = self.transport.post("grant", {"id": request["id"], "generation": self.generation, "digest": request["digest"]})
    received = self.clock()
    if (received - started > 2 or grant.get("id") != request["id"] or grant.get("digest") != request["digest"] or
        grant.get("generation") != self.generation or grant.get("leaseSeconds") != 5):
      raise ValueError("unconfirmed grant")
    if not self.safety.can_modify() or not identity_equal(request["identity"], self.identity()):
      raise ValueError("approval no longer valid")
    if self.clock() - received > 2 or self.wall() * 1000 >= request["expires"]:
      raise ValueError("grant expired before spawn")
    lease = Lease(self.transport, request, self.generation, self.safety, self.clock() + request["args"]["timeout"], self.clock, self.identity)
    lease.expires = started + 5
    lease.start()
    try:
      spawn = True

      def permitted():
        nonlocal spawn
        valid = lease.valid() and (not spawn or self.clock() - received <= 2)
        spawn = False
        return valid

      result = self.runner(request["args"]["argv"], request["args"]["cwd"], request["args"]["timeout"], permitted)
      result["output"] = redact(result.get("output", "").encode()).decode("utf-8", errors="replace")
      # Keep JSON output below the wire limit even for control-character flooding.
      while len(canonical(result)) > 60000:
        result["output"] = result["output"][:len(result["output"]) // 2]
        result["truncated"] = True
      return result
    finally:
      lease.stop.set()

  def dispatch(self, request):
    self.validate(request)
    if request["id"] in self.seen or len(self.seen) >= 1024:
      raise ValueError("request replay or generation full")
    self.seen.add(request["id"])
    try:
      if request["kind"] == "command":
        return self.command(request)
      if request["kind"] == "identity":
        return {"ok": True, "identity": self.observe_identity(), "startupIdentity": self.startup_identity,
                "runningCode": "not established by disk checkout", "source": "device"}
      return self.files.execute(request["kind"], request["args"], self.safety)
    except Exception:
      # Do not serialize exceptions: HTTP/library messages can contain credentials.
      return {"ok": False, "error": "device request refused or unconfirmed", "source": "device"}

  def step(self):
    if self.pending is not None:
      reply = self.transport.post("result", self.pending)
      if reply.get("ok") is not True:
        raise ValueError("result not acknowledged")
      self.pending = None
    reply = self.transport.post("poll", {"protocol": 1, "generation": self.generation, "bootId": self.boot_id,
                                         "identity": self.observe_identity(cached=True), "state": self.safety.snapshot()})
    request = reply.get("request")
    if request is not None:
      result = self.dispatch(request)
      self.pending = {"id": request["id"], "generation": self.generation, "result": result}
    return 2 if request is not None or reply.get("pollAfter") == 2 else 15

  def uncertain(self):
    # Never carry executable work across a failed exchange. Server invalidates it.
    self.generation = str(uuid.uuid4())
    self.pending = None
    self.seen.clear()


def main(stop=None):
  try:
    os.nice(10)
  except OSError:
    pass
  stop = stop or threading.Event()
  while not stop.is_set():
    try:
      config = load_config()
    except (OSError, ValueError):
      config = None
    if not config or not config["enabled"] or not config.get("deviceId"):
      stop.wait(30)
      continue
    safety = Safety()
    native_stop = threading.Event()
    native_thread = safety.start(native_stop)
    try:
      with open("/proc/sys/kernel/random/boot_id") as stream:
        boot_id = str(uuid.UUID(stream.read(64).strip()))
      device = Device(Transport(config["token"]), safety, boot_id=boot_id)
      failures = 0
      while not stop.is_set():
        try:
          if safety.offroad() and load_config() != config:
            break
          delay = device.step()
          failures = 0
        except Exception:
          device.uncertain()
          failures = min(failures + 1, 6)
          delay = min(60, 2 ** failures) * random.uniform(0.8, 1.2)
        stop.wait(delay)
    finally:
      native_stop.set()
      native_thread.join(timeout=1)


if __name__ == "__main__":
  main()
