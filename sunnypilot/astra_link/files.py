"""Descriptor-relative, no-follow diagnostics; selected recordings need permission."""

import base64
import os
import re
import stat

from sunnypilot.astra_link.core import MAX_CHUNK, canonical

ROOTS = ("/data/openpilot", "/data/log", "/data/community/crashes", "/data/funnypilot_triage")
RECORDINGS = ("/data/media/0/realdata", "/data/funnypilot_feedback")
SECRET = re.compile(rb'(?i)((?:authorization|bearer|token|password|passwd|secret|api[_-]?key)[\s"\x27:=]+)[^\s,"\x27}\r\n]+')
HEX_TOKEN = re.compile(rb"\b[a-f0-9]{64}\b", re.I)


def redact(data):
  data = re.sub(rb"(?i)(authorization[\s\"':=]+)[^\r\n]+", rb"\1[REDACTED]", data)
  data = re.sub(rb"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", b"[REDACTED KEY]", data, flags=re.S)
  return HEX_TOKEN.sub(b"[REDACTED]", SECRET.sub(rb"\1[REDACTED]", data))


def safe_name(name):
  lowered = name.lower()
  return (bool(name) and not name.startswith(".") and name not in (".", "..") and
          not any(word in lowered for word in ("cloud.json", "credential", "secret", "token", "password", "private", "id_rsa", "id_ed25519")) and
          lowered not in ("config.json", "receipts.json", "params", "environ", "environment", "env", "authorized_keys") and
          not lowered.endswith((".pem", ".key", ".p12", ".pfx")))


def integer(args, key, default, low, high):
  value = args.get(key, default)
  if type(value) is not int or not low <= value <= high:
    raise ValueError("invalid " + key)
  return value


class Files:
  def __init__(self, roots=ROOTS, recordings=RECORDINGS):
    self.roots, self.recordings = roots, recordings

  def locate(self, path, allow_recording=False):
    if not isinstance(path, str) or len(path) > 1024 or not path.startswith("/") or "\x00" in path:
      raise ValueError("invalid path")
    if any(part in (".", "..") for part in path.split("/")) or "//" in path:
      raise ValueError("path traversal")
    for root in (*self.roots, *self.recordings):
      if path == root or path.startswith(root + "/"):
        recording = root in self.recordings
        if recording and not allow_recording:
          raise ValueError("recording permission required")
        parts = path[len(root):].strip("/").split("/") if path != root else []
        if not all(safe_name(part) for part in parts):
          raise ValueError("private path")
        return root, parts, recording
    raise ValueError("path outside diagnostic roots")

  def open(self, path, directory=False, allow_recording=False):
    root, parts, _ = self.locate(path, allow_recording)
    # Walk from / so even a symlink replacing a configured root is rejected.
    components = root.strip("/").split("/") + parts
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
      for index, part in enumerate(components):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if index < len(components) - 1 or directory:
          flags |= os.O_DIRECTORY
        child = os.open(part, flags, dir_fd=fd)
        os.close(fd)
        fd = child
      mode = os.fstat(fd).st_mode
      if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError("not a regular diagnostic file")
      return fd
    except BaseException:
      os.close(fd)
      raise

  def raw(self, path, offset, length, allow_recording=False, tail=False):
    fd = self.open(path, allow_recording=allow_recording)
    try:
      before = os.fstat(fd)
      if tail:
        offset = max(0, before.st_size - length)
      data = os.pread(fd, length, offset)
      after = os.fstat(fd)
      metadata = {"path": path, "source": "device", "offset": offset, "length": len(data), "size": before.st_size,
                  "truncated": offset > 0 or offset + len(data) < before.st_size,
                  "changed": (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)}
      return data, metadata
    finally:
      os.close(fd)

  def execute(self, kind, args, safety):
    path = args.get("path")
    allow = args.get("allowRecording", False)
    if type(allow) is not bool:
      raise ValueError("invalid recording permission")
    _, _, recording = self.locate(path, allow)
    if kind == "list_files":
      if recording:
        raise ValueError("recording listings not supported")
      limit = integer(args, "limit", 200, 1, 200)
      offset = integer(args, "offset", 0, 0, 10000)
      fd = self.open(path, directory=True)
      try:
        entries = []
        scanned = 0
        wire_bytes = 2
        with os.scandir(fd) as iterator:
          for entry in iterator:
            scanned += 1
            if scanned > offset + limit:
              return {"ok": True, "path": path, "source": "device", "offset": offset, "entries": entries,
                      "nextOffset": scanned - 1, "truncated": True, "changed": None}
            if scanned <= offset or not safe_name(entry.name):
              continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
              item = {"name": entry.name, "directory": stat.S_ISDIR(info.st_mode), "size": info.st_size}
              wire_bytes += len(canonical(item)) + 1
              if wire_bytes > 48000:
                return {"ok": True, "path": path, "source": "device", "offset": offset, "entries": entries,
                        "nextOffset": scanned - 1, "truncated": True, "changed": None}
              entries.append(item)
        return {"ok": True, "path": path, "source": "device", "offset": offset, "entries": entries,
                "nextOffset": None, "truncated": False, "changed": None}
      finally:
        os.close(fd)
    length = integer(args, "length", 4096, 1, MAX_CHUNK)
    offset = integer(args, "offset", 0, 0, 2**63 - MAX_CHUNK)
    if (recording or length > 4096) and not safety.offroad():
      raise ValueError("offroad required")
    data, result = self.raw(path, offset, length, allow, kind == "tail_log")
    if (recording or length > 4096) and not safety.offroad():
      raise ValueError("offroad permission expired")
    clean = redact(data)
    if len(clean) > MAX_CHUNK:
      clean = clean[:MAX_CHUNK]
      result["truncated"] = True
    result.update(ok=True, encoding="base64", data=base64.b64encode(clean).decode("ascii"), redacted=clean != data)
    return result
