"""Bounded subprocess supervision, identity probes and lease cancellation."""

import os
import selectors
import signal
import subprocess
import threading
import time

from sunnypilot.astra_link.core import MAX_CHUNK

ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent", "GIT_OPTIONAL_LOCKS": "0",
       "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


def run_process(argv, cwd, timeout, permitted=lambda: True, cap=MAX_CHUNK):
  if not permitted():
    return {"ok": False, "error": "permission expired", "output": "", "truncated": False}
  proc = subprocess.Popen(argv, cwd=cwd, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
  output = bytearray()
  total = 0
  error = None
  deadline = time.monotonic() + timeout
  selector = None
  try:
    selector = selectors.DefaultSelector()
    os.set_blocking(proc.stdout.fileno(), False)
    selector.register(proc.stdout, selectors.EVENT_READ)
    while selector.get_map():
      if not permitted():
        error = "permission expired"
      if time.monotonic() >= deadline:
        error = "timeout"
      if error:
        break
      for key, _ in selector.select(0.05):
        data = os.read(key.fd, 8192)
        if not data:
          selector.unregister(key.fileobj)
        else:
          total += len(data)
          output.extend(data[:max(0, cap - len(output))])
      if proc.poll() is not None and not selector.get_map():
        break
    if not error:
      while proc.poll() is None:
        if not permitted() or time.monotonic() >= deadline:
          error = "permission expired or timeout"
          break
        time.sleep(0.02)
  finally:
    # Kill the entire group even if the immediate child already exited.
    try:
      os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
      pass
    try:
      proc.wait(timeout=2)
    finally:
      if selector is not None:
        selector.close()
      proc.stdout.close()
  result = {"ok": error is None and proc.returncode == 0, "exitCode": proc.returncode,
            "output": output.decode("utf-8", errors="replace"), "truncated": total > cap, "source": "device"}
  if error:
    result["error"] = error
  return result


def probe_identity(root="/data/openpilot"):
  identity = {"branch": None, "commit": None, "dirty": None, "version": None}
  commands = {"branch": ["symbolic-ref", "--quiet", "--short", "HEAD"], "commit": ["rev-parse", "--verify", "HEAD"],
              "dirty": ["status", "--porcelain", "--untracked-files=normal"]}
  for field, args in commands.items():
    try:
      result = run_process(["/usr/bin/git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args], root, 1)
      if result["ok"]:
        identity[field] = bool(result["output"]) if field == "dirty" else result["output"].strip() or None
        if result["truncated"] and field != "dirty":
          identity[field] = None
    except (OSError, ValueError, subprocess.SubprocessError):
      pass
  try:
    from sunnypilot.astra_link.files import Files
    data, _ = Files(roots=(root,)).raw(root + "/FUNNYPILOT_VERSION", 0, 128)
    identity["version"] = data.decode("utf-8").strip() or None
  except (OSError, ValueError, UnicodeError):
    pass
  return identity


class Lease:
  def __init__(self, transport, request, generation, safety, deadline, clock=time.monotonic, identity=probe_identity):
    self.transport, self.request, self.generation = transport, request, generation
    self.safety, self.deadline, self.clock = safety, deadline, clock
    self.identity = identity
    self.expires = clock() + 5
    self.stop = threading.Event()
    self.failed = threading.Event()

  def valid(self):
    return not self.failed.is_set() and self.safety.offroad() and self.clock() < min(self.expires, self.deadline)

  def renew(self):
    while not self.stop.wait(2):
      started = self.clock()
      try:
        identity = self.identity()
        from sunnypilot.astra_link.core import identity_equal
        if not identity_equal(identity, self.request["identity"]) or not self.valid():
          raise ValueError("approval identity changed")
        reply = self.transport.post("lease", {"id": self.request["id"], "generation": self.generation,
                                               "identity": identity, "state": self.safety.snapshot()})
        if reply.get("leaseSeconds") != 5 or self.clock() - started > 2 or not self.valid():
          raise ValueError("lease denied")
        self.expires = started + 5
      except Exception:
        self.failed.set()
        return

  def start(self):
    threading.Thread(target=self.renew, daemon=True, name="astra-lease").start()
