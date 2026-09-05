"""Wire validation, private persistence and fixed-origin HTTPS transport."""

import hashlib
import json
import os
import re
import stat
import time
import uuid

ORIGIN = "https://astra.jgray.cc"
CONFIG = "/data/astra_link/config.json"
JOURNAL = "/data/astra_link/receipts.json"
MAX_WIRE = 65536
MAX_CHUNK = 32768
IDENTITY_KEYS = ("branch", "commit", "dirty", "version")


def canonical(value):
  return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(kind, args):
  return hashlib.sha256(canonical({"kind": kind, "args": args})).hexdigest()


def identity_equal(left, right):
  for identity in (left, right):
    if not isinstance(identity, dict) or any(key not in identity for key in IDENTITY_KEYS):
      return False
    if not isinstance(identity["commit"], str) or not re.fullmatch(r"[a-f0-9]{40}", identity["commit"]):
      return False
    if type(identity["dirty"]) is not bool:
      return False
    if any(identity[key] is not None and not isinstance(identity[key], str) for key in ("branch", "version")):
      return False
  return all(left[key] == right[key] for key in IDENTITY_KEYS)


def private_directory(path, create=False):
  if not os.path.isabs(path) or any(p in (".", "..") for p in path.split("/")):
    raise ValueError("invalid private path")
  parts = os.path.dirname(path).strip("/").split("/")
  fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
  try:
    for index, part in enumerate(parts):
      if create and index == len(parts) - 1:
        try:
          os.mkdir(part, 0o700, dir_fd=fd)
        except FileExistsError:
          pass
      child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
      os.close(fd)
      fd = child
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
      raise ValueError("unsafe private directory")
    return fd
  except BaseException:
    os.close(fd)
    raise


def private_read(path, limit=MAX_WIRE):
  directory_fd = private_directory(path)
  try:
    fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
  finally:
    os.close(directory_fd)
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
      raise ValueError("unsafe private file")
    data = os.read(fd, limit + 1)
    if len(data) > limit:
      raise ValueError("private file too large")
    return json.loads(data)
  finally:
    os.close(fd)


def private_write(path, value):
  data = canonical(value)
  if len(data) > MAX_WIRE:
    raise ValueError("private storage full")
  directory_fd = private_directory(path, create=True)
  temp = ".tmp-" + uuid.uuid4().hex
  try:
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "wb") as stream:
      stream.write(data)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temp, os.path.basename(path), src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    os.fsync(directory_fd)
  finally:
    try:
      os.unlink(temp, dir_fd=directory_fd)
    except FileNotFoundError:
      pass
    os.close(directory_fd)


def load_config(path=CONFIG):
  value = private_read(path)
  if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
    raise ValueError("invalid config")
  token = value.get("token")
  if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
    raise ValueError("invalid credential")
  if "deviceId" in value:
    device_id = value["deviceId"]
    if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device_id):
      raise ValueError("invalid device id")
  return value


class Journal:
  """Never evict receipts: full/corrupt storage requires explicit local maintenance."""

  def __init__(self, path=JOURNAL):
    self.path = path

  def record(self, request):
    try:
      value = private_read(self.path)
    except FileNotFoundError:
      value = {"version": 1, "receipts": {}}
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("receipts"), dict):
      raise ValueError("invalid receipt journal")
    receipts = value["receipts"]
    if any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in receipts.items()):
      raise ValueError("invalid receipt journal")
    if request["id"] in receipts or len(receipts) >= 256:
      raise ValueError("receipt replay or journal full")
    receipts[request["id"]] = request["digest"]
    private_write(self.path, value)


class Transport:
  def __init__(self, token=None):
    import requests
    self.session = requests.Session()
    self.session.trust_env = False
    self.token = token

  def post(self, route, value):
    if route not in ("pair", "poll", "result", "grant", "lease"):
      raise ValueError("invalid route")
    data = canonical(value)
    if len(data) > MAX_WIRE:
      raise ValueError("request too large")
    headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
    if self.token:
      headers["Authorization"] = "Bearer " + self.token
    started = time.monotonic()
    with self.session.post(ORIGIN + "/device/" + route, data=data, headers=headers,
                           timeout=(2, 2), allow_redirects=False, stream=True) as response:
      if response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
        raise ValueError("link HTTP failure")
      chunks = bytearray()
      # One-byte yields bound drip-fed bodies as well as ordinary large responses.
      for chunk in response.iter_content(1):
        chunks.extend(chunk)
        if len(chunks) > MAX_WIRE or time.monotonic() - started > 5:
          raise ValueError("response too large or slow")
      value = json.loads(chunks)
      if not isinstance(value, dict):
        raise ValueError("invalid response")
      return value
