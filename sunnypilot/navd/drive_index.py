"""FunnyPilot v3.6.8 — the drive catalogue behind the web dashboard.

WHAT THIS IS FOR. loggerd already writes everything a playback UI needs; nothing
here records anything new. A drive is a directory of one-minute SEGMENTS under
`/data/media/0/realdata`, each holding a decimated log and up to four video
files. This module turns that tree into something a browser can list, scrub and
delete, and it does so under one hard constraint:

    THE DEVICE IS DRIVING A CAR. Every byte of RAM and every millisecond of CPU
    spent here is taken from a process that has somewhere to be.

That constraint shapes every decision below, so they are worth stating as rules
rather than leaving as choices:

  1. **NOTHING IS EVER READ WHOLE.** The catalogue is a directory scan
     (`os.scandir` + `stat`), never a log parse. The timeline extractor streams
     capnp events one at a time out of a zstd stream reader and keeps only the
     current bucket. Peak RSS is a few hundred kilobytes regardless of how long
     the drive was.
  2. **EVERY EXPENSIVE ANSWER IS COMPUTED ONCE AND CACHED BESIDE THE DATA.**
     A parsed segment writes `fp_timeline.json` (a few kB) into its own
     directory. It is derived, disposable, and deleted with the segment.
  3. **VIDEO IS NEVER TRANSCODED.** `qcamera.ts` is already H.264 in MPEG-TS at
     526x330 and 256 kbit/s — about 1.9 MB per minute — which is *exactly* an
     HLS media segment. The playlist is generated as text; the segments are
     served as files. Transcoding HEVC on this SoC while it drives is not a
     trade worth discussing.
  4. **EVERY PATH IS VALIDATED AGAINST THE ROOT.** These names arrive from a
     browser and are used to build filesystem paths and, for deletion, to
     remove trees. `_safe_segment_dir` is the only way to turn a name into a
     path, and it resolves and re-checks containment.

Import-light on purpose: stdlib only at module scope. `capnp`/`zstandard` are
imported lazily INSIDE the extractor, so the catalogue, the playlist and the
delete path all work on a machine where those are missing, and so importing
this module can never fail on the webserver's startup path.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time

# Where loggerd puts drives. Every entry point takes `root=None` and resolves
# it HERE, at call time, rather than as a default argument — a module constant
# used as a default is bound when the `def` runs, so it can never be redirected
# afterwards and the whole module becomes untestable against a fixture tree.
# Found by a smoke test that pointed this at a temporary directory and got an
# empty catalogue back.
REALDATA_ROOT = "/data/media/0/realdata"

# A route name is `<dongle-or-local>|<timestamp>--<segment>` or, offline,
# `<timestamp>--<segment>`. Anchored and character-limited because it is used to
# build paths; see `_safe_segment_dir`.
_SEG_RE = re.compile(r"^(?P<route>[A-Za-z0-9|_-]+)--(?P<seg>\d+)$")

# What loggerd can leave in a segment, and what each is worth to a viewer.
# `qcamera.ts` first because it is the one the browser can actually play.
VIDEO_FILES = {
  "qcamera.ts": ("road", "Road (low-res)", True),
  "fcamera.hevc": ("road_hq", "Road (full)", False),
  "ecamera.hevc": ("wide", "Wide road", False),
  "dcamera.hevc": ("driver", "Driver", False),
}
LOG_FILES = ("qlog.zst", "rlog.zst", "qlog.bz2", "rlog.bz2")

TIMELINE_NAME = "fp_timeline.json"
TIMELINE_VERSION = 1

# One row per second of drive. Finer buys nothing on a scrub bar a few hundred
# pixels wide and costs proportionally more JSON over the wire and in the
# browser's heap.
BUCKET_S = 1.0

# Control authority, in the order a driver hands it over.
AUTH_MANUAL = 0
AUTH_LATERAL = 1
AUTH_LONG = 2
AUTH_FULL = 3
AUTH_NAMES = {AUTH_MANUAL: "manual", AUTH_LATERAL: "lateral",
              AUTH_LONG: "longitudinal", AUTH_FULL: "full"}


# ── the catalogue ───────────────────────────────────────────────────────────

def _safe_segment_dir(root: str, name: str) -> str | None:
  """Resolve a segment directory name, or None if it is not one of ours.

  THE ONLY WAY A BROWSER-SUPPLIED NAME BECOMES A PATH, and it is three checks
  rather than the obvious two.

  1. the name matches `_SEG_RE` — no separators, no dots, no traversal;
  2. the entry is NOT A SYMLINK. loggerd writes real directories, so a link
     here is never legitimate;
  3. the resolved path is still directly inside the resolved root.

  **CHECK 2 IS THE ONE A TEST FOUND MISSING.** With only 1 and 3, a symlink at
  `realdata/evil--0` pointing at a SIBLING of realdata passes cleanly: the
  resolved target's parent is the same directory as the resolved root, so the
  containment test is satisfied while the path is somewhere else entirely. That
  is a delete-as-root primitive pointed at an arbitrary tree, and it read as
  correct — which is precisely why `test_a_symlink_out_of_the_root_is_refused`
  exists and why it drives the validator rather than the delete.
  """
  if not name or not _SEG_RE.match(name):
    return None
  real_root = os.path.realpath(root)
  joined = os.path.join(real_root, name)
  if os.path.islink(joined):
    return None
  path = os.path.realpath(joined)
  if os.path.dirname(path) != real_root:
    return None
  return path


def parse_segment_name(name: str):
  """('<route>', <segment index>) or None."""
  m = _SEG_RE.match(name or "")
  if not m:
    return None
  return m.group("route"), int(m.group("seg"))


def route_started_at(route: str) -> float | None:
  """Wall-clock start of a route from its own name, seconds since the epoch.

  openpilot names routes `<id>|YYYY-MM-DD--HH-MM-SS`. Returns None rather than
  guessing when the name does not carry one — a drive with an unreadable name
  is still a drive, and the directory mtime is the fallback the caller uses.
  """
  tail = route.split("|")[-1]
  # A WALL CLOCK value is what is wanted here — a calendar timestamp for a
  # human reading a list, not an interval. The monotonic rule in CLAUDE.md is
  # about AGEING data; this is naming a moment.
  try:
    return time.mktime(time.strptime(tail, "%Y-%m-%d--%H-%M-%S"))
  except (ValueError, OverflowError):
    return None


def _dir_size(path: str) -> int:
  """Bytes in one segment directory, one level deep. `scandir` + the stat it
  already carries, so this is one syscall per file rather than two."""
  total = 0
  try:
    with os.scandir(path) as it:
      for e in it:
        try:
          st = e.stat(follow_symlinks=False)
          if stat.S_ISREG(st.st_mode):
            total += st.st_size
        except OSError:
          continue
  except OSError:
    return 0
  return total


def scan_routes(root: str | None = None) -> list[dict]:
  """Every drive on the device, newest first. NO LOG IS OPENED.

  This is what the list page runs, so it has to stay a directory walk. On a
  device holding a few hundred segments that is a handful of milliseconds; the
  same answer from the logs would be minutes and hundreds of megabytes.
  """
  root = root or REALDATA_ROOT
  routes: dict[str, dict] = {}
  try:
    entries = list(os.scandir(root))
  except OSError:
    return []

  for e in entries:
    if not e.is_dir(follow_symlinks=False):
      continue
    parsed = parse_segment_name(e.name)
    if parsed is None:
      continue
    route, idx = parsed
    r = routes.setdefault(route, {
      "route": route, "segments": [], "bytes": 0,
      "cameras": set(), "mtime": 0.0, "has_timeline": True,
    })
    r["segments"].append(idx)
    r["bytes"] += _dir_size(e.path)
    try:
      r["mtime"] = max(r["mtime"], e.stat().st_mtime)
    except OSError:
      pass
    for fname, (key, _label, _playable) in VIDEO_FILES.items():
      if os.path.exists(os.path.join(e.path, fname)):
        r["cameras"].add(key)
    if not os.path.exists(os.path.join(e.path, TIMELINE_NAME)):
      r["has_timeline"] = False

  out = []
  for r in routes.values():
    segs = sorted(r["segments"])
    started = route_started_at(r["route"])
    out.append({
      "route": r["route"],
      "segments": segs,
      "n_segments": len(segs),
      "bytes": r["bytes"],
      # Length is segment COUNT, not a log timestamp: the last segment is
      # usually short and the honest answer needs the log. Named `duration_est`
      # so the UI cannot present a guess as a measurement.
      "duration_est": len(segs) * 60,
      "cameras": sorted(r["cameras"]),
      "started_at": started,
      "mtime": r["mtime"],
      "has_timeline": r["has_timeline"],
    })
  out.sort(key=lambda d: (d["started_at"] or d["mtime"]), reverse=True)
  return out


def realdata_status(root: str | None = None) -> dict:
  """WHY the catalogue is empty, when it is empty.

  **AN EMPTY LIST HAS FOUR CAUSES AND `scan_routes` CANNOT TELL THEM APART.**
  It swallows OSError and returns `[]` for a missing directory, an unreadable
  one, a genuinely empty one, and one full of files that do not parse as
  segments. On screen those are the same picture — which is the exact trap this
  repo has hit before (v3.6.5's LANE signal, whose failure mode WAS its healthy
  reading), and it cost a round trip to diagnose the first time the Drives tab
  came up empty.

  So the API says which. This is diagnosis, not control: nothing branches on
  it, it is a directory stat and a listing head, and it is the difference
  between "you have no recordings" and "I cannot read /data/media/0/realdata".
  """
  root = root or REALDATA_ROOT
  st = {"root": root, "exists": False, "readable": False,
        "entries": 0, "segments": 0, "error": None, "sample": []}
  if not os.path.isdir(root):
    st["error"] = "the log directory does not exist"
    return st
  st["exists"] = True
  try:
    names = os.listdir(root)
  except OSError as e:
    st["error"] = f"cannot read the log directory: {e.strerror}"
    return st
  st["readable"] = True
  st["entries"] = len(names)
  st["segments"] = sum(1 for n in names if parse_segment_name(n))
  # A few names, so a format that stopped parsing is visible rather than
  # inferred. Bounded because this goes over the wire on every page load.
  st["sample"] = sorted(names)[:5]
  if st["entries"] and not st["segments"]:
    st["error"] = "the log directory has files, but none are named like segments"
  return st


def route_segments(route: str, root: str | None = None) -> list[int]:
  root = root or REALDATA_ROOT
  segs = []
  try:
    with os.scandir(root) as it:
      for e in it:
        p = parse_segment_name(e.name)
        if p and p[0] == route and e.is_dir(follow_symlinks=False):
          segs.append(p[1])
  except OSError:
    return []
  return sorted(segs)


def segment_path(route: str, seg: int, root: str | None = None) -> str | None:
  root = root or REALDATA_ROOT
  return _safe_segment_dir(root, f"{route}--{int(seg)}")


def delete_route(route: str, root: str | None = None) -> tuple[int, int]:
  """Remove every segment of one drive. Returns (segments removed, bytes freed).

  EVERY PATH GOES THROUGH `_safe_segment_dir`, INDIVIDUALLY. Building one glob
  and handing it to a recursive delete is how a name like `..` or a planted
  symlink turns a housekeeping feature into a device wipe — and this runs as
  root. A name that does not resolve to a direct child of the real root is
  skipped, not sanitised: there is no repair that is safer than refusing.
  """
  root = root or REALDATA_ROOT
  removed = freed = 0
  for seg in route_segments(route, root):
    path = _safe_segment_dir(root, f"{route}--{seg}")
    if path is None or not os.path.isdir(path):
      continue
    freed += _dir_size(path)
    try:
      shutil.rmtree(path)
      removed += 1
    except OSError:
      freed -= _dir_size(path)
  return removed, freed


def storage_stats(path: str | None = None) -> dict:
  path = path or REALDATA_ROOT
  try:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
  except OSError:
    return {"total": 0, "free": 0, "used": 0, "drives": 0}
  drives = sum(r["bytes"] for r in scan_routes(path))
  return {"total": total, "free": free, "used": total - free, "drives": drives}


# ── the HLS playlist ────────────────────────────────────────────────────────

def hls_playlist(route: str, segments: list[int], seg_seconds: float = 60.0,
                 url_for=None) -> str:
  """An HLS playlist over the segments loggerd already wrote.

  THE POINT IS THAT NOTHING IS PRODUCED. `qcamera.ts` is H.264 in MPEG-TS, one
  file per minute — which is the container and the segmentation HLS specifies.
  So the "encoder" here is a text file, the CPU cost is zero, and the browser
  streams segments over ordinary range requests instead of us holding a drive
  in memory.

  `EXT-X-PLAYLIST-TYPE:VOD` plus `ENDLIST` is what makes the player treat it as
  a finished recording it may seek freely inside, rather than a live stream it
  must tail.
  """
  url_for = url_for or (lambda s: f"seg/{s}/qcamera.ts")
  lines = [
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-PLAYLIST-TYPE:VOD",
    f"#EXT-X-TARGETDURATION:{int(seg_seconds) + 1}",
    "#EXT-X-MEDIA-SEQUENCE:0",
  ]
  for s in segments:
    lines.append(f"#EXTINF:{seg_seconds:.3f},")
    lines.append(url_for(s))
  lines.append("#EXT-X-ENDLIST")
  return "\n".join(lines) + "\n"


# ── the timeline ────────────────────────────────────────────────────────────

def authority_of(lat_active: bool, long_active: bool) -> int:
  """Who is driving. The four states the ribbon is drawn from."""
  if lat_active and long_active:
    return AUTH_FULL
  if lat_active:
    return AUTH_LATERAL
  if long_active:
    return AUTH_LONG
  return AUTH_MANUAL


class _Bucket:
  """One second of drive, accumulated in place.

  Deliberately a handful of scalars and no lists. This is the only per-sample
  state the extractor holds, which is what makes peak memory independent of
  drive length.
  """
  __slots__ = ("t", "n", "v_sum", "v_max", "a_sum", "a_min", "lead_min",
               "auth", "n_auth", "steer_max")

  def __init__(self, t: int):
    self.t = t
    self.n = 0
    self.v_sum = 0.0
    self.v_max = 0.0
    self.a_sum = 0.0
    self.a_min = 0.0
    self.lead_min = -1.0
    self.steer_max = 0.0
    self.auth = [0, 0, 0, 0]
    self.n_auth = 0

  def row(self) -> list:
    n = max(self.n, 1)
    # THE AUTHORITY OF A SECOND IS ITS MOST MANUAL SAMPLE, NOT ITS MEAN. A
    # bucket containing one disengaged frame is a bucket in which the driver
    # took over, and rounding that away is the one error this readout must not
    # make. `index(max())` over a fixed order would pick the most COMMON.
    auth = AUTH_FULL
    for a in (AUTH_MANUAL, AUTH_LATERAL, AUTH_LONG):
      if self.auth[a]:
        auth = a
        break
    if not self.n_auth:
      auth = AUTH_MANUAL
    return [
      self.t, auth,
      round(self.v_sum / n, 2), round(self.v_max, 2),
      round(self.a_sum / n, 2), round(self.a_min, 2),
      round(self.lead_min, 1), round(self.steer_max, 1),
    ]


ROW_FIELDS = ["t", "auth", "v_avg", "v_max", "a_avg", "a_min", "lead", "steer"]


def extract_timeline(seg_dir: str, bucket_s: float = BUCKET_S) -> dict | None:
  """Stream one segment's qlog into a small timeline. None if it cannot be read.

  ONE PASS, ONE EVENT AT A TIME, NOTHING RETAINED. `capnp`'s `read_multiple`
  over a zstd stream reader yields events lazily, and the only state kept
  across them is the current `_Bucket` plus the finished rows — a few hundred
  bytes per minute of drive.

  BOTH IMPORTS ARE LOCAL AND GUARDED. This module is imported by the webserver
  at startup and the webserver is a manager process: an ImportError at module
  scope would take the whole thing down, and the catalogue, the playlist and
  the delete path all work fine without either library.
  """
  try:
    import capnp  # noqa: F401
    import zstandard
    from cereal import log as capnp_log
  except Exception:
    return None

  qlog = os.path.join(seg_dir, "qlog.zst")
  if not os.path.exists(qlog):
    return None

  rows: list[list] = []
  marks: list[float] = []
  cur: _Bucket | None = None
  t0 = None
  lat = lon_ = False
  v_ego = a_cmd = steer = 0.0
  lead_d = -1.0
  distance_m = 0.0
  last_t = None

  def flush():
    if cur is not None:
      rows.append(cur.row())

  try:
    with open(qlog, "rb") as fh:
      reader = zstandard.ZstdDecompressor().stream_reader(fh)
      for evt in capnp_log.Event.read_multiple(reader):
        try:
          which = evt.which()
        except Exception:
          continue
        mono = evt.logMonoTime * 1e-9
        if t0 is None:
          t0 = mono
        t = mono - t0

        if which == "carControl":
          cc = evt.carControl
          lat, lon_ = bool(cc.latActive), bool(cc.longActive)
        elif which == "carState":
          cs = evt.carState
          v_ego = float(cs.vEgo)
          steer = abs(float(cs.steeringAngleDeg))
          if last_t is not None and t > last_t:
            distance_m += v_ego * (t - last_t)
          last_t = t
        elif which == "carOutput":
          a_cmd = float(evt.carOutput.actuatorsOutput.accel)
        elif which == "radarState":
          lo = evt.radarState.leadOne
          lead_d = float(lo.dRel) if lo.status else -1.0
        elif which == "bookmarkButton":
          marks.append(round(t, 2))
          continue
        else:
          continue

        b = int(t // bucket_s)
        if cur is None or cur.t != b:
          flush()
          cur = _Bucket(b)
        cur.n += 1
        cur.v_sum += v_ego
        cur.v_max = max(cur.v_max, v_ego)
        cur.a_sum += a_cmd
        cur.a_min = min(cur.a_min, a_cmd)
        cur.steer_max = max(cur.steer_max, steer)
        if lead_d >= 0:
          cur.lead_min = lead_d if cur.lead_min < 0 else min(cur.lead_min, lead_d)
        cur.auth[authority_of(lat, lon_)] += 1
        cur.n_auth += 1
      flush()
  except Exception:
    # A truncated tail is the NORMAL case for the last segment of a drive that
    # ended with the ignition. Keep what was parsed rather than losing the
    # segment: a partial timeline is a correct timeline of a partial log.
    flush()
    if not rows:
      return None

  return {
    "v": TIMELINE_VERSION,
    "fields": ROW_FIELDS,
    "rows": rows,
    "marks": marks,
    "distance_m": round(distance_m, 1),
    "duration_s": round(rows[-1][0] * bucket_s + bucket_s, 1) if rows else 0.0,
  }


def segment_timeline(seg_dir: str, rebuild: bool = False) -> dict | None:
  """Cached `extract_timeline`. The cache lives beside the data it describes.

  Written with the same tempfile-and-rename the /dev/shm channels use, so a
  reader can never see a half-written file, and stored INSIDE the segment so
  deleting a drive deletes its derived data with it — no second index to keep
  in sync and no orphans to garbage-collect.
  """
  cache = os.path.join(seg_dir, TIMELINE_NAME)
  if not rebuild:
    try:
      with open(cache) as f:
        got = json.load(f)
      if isinstance(got, dict) and got.get("v") == TIMELINE_VERSION:
        return got
    except (OSError, ValueError):
      pass

  data = extract_timeline(seg_dir)
  if data is None:
    return None
  try:
    tmp = cache + ".tmp"
    with open(tmp, "w") as f:
      json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, cache)
  except OSError:
    pass
  return data


def route_timeline(route: str, root: str | None = None, limit: int | None = None) -> dict:
  """Stitch the per-segment timelines of one drive into one track.

  Segment `n` starts at `n * 60` seconds by construction — that is what
  loggerd's fixed SEGMENT_LENGTH buys, and it is why this can offset rows
  arithmetically instead of reading a clock out of every log.

  `limit` bounds how many segments may be PARSED in one request so a first
  visit to a long drive cannot occupy the CPU for a minute. Already-cached
  segments are free and never count against it; the response says which are
  still pending and the client asks again.
  """
  root = root or REALDATA_ROOT
  segs = route_segments(route, root)
  rows: list[list] = []
  marks: list[float] = []
  pending: list[int] = []
  distance = 0.0
  parsed = 0

  for s in segs:
    path = _safe_segment_dir(root, f"{route}--{s}")
    if path is None:
      continue
    cached = os.path.exists(os.path.join(path, TIMELINE_NAME))
    if not cached and limit is not None and parsed >= limit:
      pending.append(s)
      continue
    if not cached:
      parsed += 1
    tl = segment_timeline(path)
    if tl is None:
      continue
    off = s * 60
    for r in tl["rows"]:
      rows.append([r[0] + off] + r[1:])
    marks.extend(round(m + off, 2) for m in tl.get("marks", []))
    distance += float(tl.get("distance_m", 0.0))

  rows.sort(key=lambda r: r[0])
  return {
    "route": route,
    "fields": ROW_FIELDS,
    "rows": rows,
    "marks": sorted(marks),
    "distance_m": round(distance, 1),
    "duration_s": (rows[-1][0] + 1) if rows else 0.0,
    "pending": pending,
    "segments": segs,
  }
