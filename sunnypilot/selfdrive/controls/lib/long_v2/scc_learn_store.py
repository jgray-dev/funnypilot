"""FunnyPilot v3.5.0 — the learned-corner store: our own map, built by driving.

THE IDEA. SCC-M asks OSM how fast a bend can be taken and is wrong often
enough that it needs a vision veto. But the car already knows the answer for
any road it has driven: the speed it actually went through the bend. Record
that, keyed by position and heading, and after one pass a road has a corner
map that owes nothing to OSM's geometry — and unlike OSM it is derived from
this car, this driver and this tyre set. On a commute it is strictly better
information than the map.

WHAT THIS MODULE IS: only the persistence and the geometry index. The decision
about WHEN something is worth recording lives in scc_learn.py, and the decision
about how much authority a learned point gets lives in scc_fusion.py. Keeping
those apart is what makes the hard parts testable without a car.

STORAGE SHAPE
  * A cell key is (lat_cell, lon_cell, heading_octant) at CELL_DEG resolution
    (~22 m). The octant is in the KEY, not just a field, because a bend taken
    northbound and the same tarmac taken southbound are different approaches
    and deserve separate estimates.
  * A coarse index (COARSE_DEG, ~1.1 km) maps to the fine keys inside it, so a
    lookup probes 9 coarse cells rather than scanning 25,000 records. At 20 Hz
    that difference is the whole feasibility of the feature.

PERSISTENCE IS AN APPEND-ONLY JOURNAL, COMPACTED AT STARTUP.
plannerd is `only_onroad`, so it restarts every drive — which means compaction
has a natural, free moment to happen and NO BACKGROUND THREAD IS EVER NEEDED.
That is deliberate: v3.4.5 shipped a startup task that shelled out to `git gc`
and OOM-killed the device (see the v3.4.6 post-mortem), and the rule that came
out of it was that anything running beside a moving car must be bounded in
MEMORY, not merely in time. Appending a handful of short lines every FLUSH_S is
bounded by construction; a full rewrite happens once, before the car moves.

EVICTION KEEPS THE ROADS YOU ACTUALLY DRIVE. When the store is over
MAX_RECORDS it is sorted by visit count first and last-seen second, and the
tail is dropped — so the road you take to work every morning outlives a road
you drove once on holiday. That ordering is the explicit requirement.

EVERY OPERATION IS BEST-EFFORT. This runs in plannerd, next to the code that
decides how hard to brake. A full disk, a corrupt line, a truncated write, a
read-only filesystem: all of them must degrade to "no learned data" and none of
them may raise into the planner. Losing the file entirely is an acceptable
outcome — the feature simply relearns.

Import-light (stdlib only), so it tests without the openpilot environment.
"""
import json
import math
import os
import tempfile
import time

STORE_DIR = "/data/funnypilot_scc_learn"
STORE_NAME = "corners.jsonl"

# ~22 m at the equator, and 18 m of longitude at 35 deg latitude. Fine enough to
# place a corner entry, coarse enough that GPS noise lands in the same cell.
CELL_DEG = 1.0 / 5000.0
# ~1.1 km buckets for the lookup index.
COARSE_DEG = 1.0 / 100.0
OCTANTS = 8

MAX_RECORDS = 25_000          # ~2.2 MB compacted; far more corners than a lifetime
MAX_JOURNAL_BYTES = 8 << 20   # hard cap on the on-disk journal between compactions
# The load happens on the first onroad planner frame — at IGNITION, with the car
# stationary, not at engage. It is still the one unbounded-looking piece of work
# in the module, so it gets an explicit line budget rather than trusting the byte
# cap above to imply one. Past this the journal is truncated to what was read,
# which costs the oldest un-compacted appends and nothing else.
MAX_JOURNAL_LINES = 120_000
MIN_FREE_BYTES = 512 << 20    # never write when /data is this tight; deleter owns that space
FLUSH_S = 60.0                # batch dirty records; a long drive writes a few tens of KB

# How the estimate moves when a corner is seen again. Asymmetric ON PURPOSE:
# rising (we took it faster than we thought) is adopted quickly, falling (we
# took it slower) is adopted slowly. A learned cap can only ever slow the car,
# so the expensive failure is nuisance braking, and this biases away from it.
ALPHA_UP = 0.5
ALPHA_DOWN = 0.2

# A grid has edges, and a corner is as likely to sit on one as anywhere else.
# Without a merge step, two visits to the SAME bend that land either side of a
# cell boundary become two records with one visit each — which is not merely
# untidy: visit count is what the eviction rule keeps, so a boundary corner on
# the commute would be filed as two holiday corners and thrown away first.
# On write only (once per corner event, not per frame) we fold into an existing
# nearby record whose heading agrees.
MERGE_M = 30.0
MERGE_BEARING_DEG = 45.0


def cell_of(lat: float, lon: float) -> tuple[int, int]:
  return int(math.floor(lat / CELL_DEG)), int(math.floor(lon / CELL_DEG))


def coarse_of(lat: float, lon: float) -> tuple[int, int]:
  return int(math.floor(lat / COARSE_DEG)), int(math.floor(lon / COARSE_DEG))


def octant_of(bearing_deg: float) -> int:
  return int(math.floor((bearing_deg % 360.0) / (360.0 / OCTANTS))) % OCTANTS


def bearing_delta(a: float, b: float) -> float:
  """Smallest absolute angle between two headings, degrees."""
  d = abs((a - b) % 360.0)
  return min(d, 360.0 - d)


class Corner:
  __slots__ = ("lat", "lon", "bearing", "v", "n", "t", "flags")

  def __init__(self, lat, lon, bearing, v, n=1, t=0.0, flags=0):
    self.lat, self.lon, self.bearing = lat, lon, bearing
    self.v, self.n, self.t, self.flags = v, n, t, flags

  def to_json(self, key: str) -> str:
    return json.dumps({"k": key, "la": round(self.lat, 6), "lo": round(self.lon, 6),
                       "br": round(self.bearing, 1), "v": round(self.v, 2),
                       "n": self.n, "t": round(self.t, 0), "f": self.flags},
                      separators=(",", ":"))

  @staticmethod
  def from_obj(d):
    return d["k"], Corner(float(d["la"]), float(d["lo"]), float(d["br"]),
                          float(d["v"]), int(d.get("n", 1)),
                          float(d.get("t", 0.0)), int(d.get("f", 0)))


def key_for(lat: float, lon: float, bearing: float) -> str:
  cy, cx = cell_of(lat, lon)
  return f"{cy},{cx},{octant_of(bearing)}"


class LearnStore:
  """In-memory corner map with a compacted-on-startup journal behind it."""

  def __init__(self, directory: str = STORE_DIR, name: str = STORE_NAME,
               max_records: int = MAX_RECORDS, autoload: bool = True):
    self.path = os.path.join(directory, name)
    self.directory = directory
    self.max_records = max_records
    self.corners: dict[str, Corner] = {}
    self._index: dict[tuple[int, int], set[str]] = {}
    self._dirty: set[str] = set()
    self._last_flush = 0.0
    self._journal_full = False
    self.loaded = False
    if autoload:
      self.load()

  # ── index ───────────────────────────────────────────────────────────────

  def _index_add(self, key: str, c: Corner) -> None:
    self._index.setdefault(coarse_of(c.lat, c.lon), set()).add(key)

  def _reindex(self) -> None:
    self._index = {}
    for key, c in self.corners.items():
      self._index_add(key, c)

  # ── load / compact ──────────────────────────────────────────────────────

  def load(self) -> None:
    """Read the journal, last-write-wins per key, evict, rewrite compacted.

    Called once at plannerd start. A corrupt line is skipped rather than
    fatal — a half-written record from a power cut must cost one corner, not
    the whole map.
    """
    self.loaded = True
    try:
      with open(self.path) as f:
        for i, line in enumerate(f):
          if i >= MAX_JOURNAL_LINES:
            break
          line = line.strip()
          if not line:
            continue
          try:
            key, c = Corner.from_obj(json.loads(line))
          except Exception:
            continue
          self.corners[key] = c
    except Exception:
      self.corners = {}

    self._evict()
    self._reindex()
    # rewrite so the journal starts each drive at its compacted size
    self._rewrite()

  def _evict(self) -> None:
    """Keep the most-driven. Visit count first, recency as the tie-break —
    the road you take every morning must outlive the one you drove once."""
    if len(self.corners) <= self.max_records:
      return
    ordered = sorted(self.corners.items(), key=lambda kv: (kv[1].n, kv[1].t), reverse=True)
    self.corners = dict(ordered[:self.max_records])

  def _have_space(self) -> bool:
    try:
      st = os.statvfs(self.directory if os.path.isdir(self.directory) else "/data")
      return st.f_bavail * st.f_frsize > MIN_FREE_BYTES
    except Exception:
      return False

  def _rewrite(self) -> None:
    """Atomic full dump. Startup only."""
    try:
      os.makedirs(self.directory, exist_ok=True)
      if not self._have_space():
        return
      fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".corners")
      try:
        with os.fdopen(fd, "w") as f:
          for key, c in self.corners.items():
            f.write(c.to_json(key) + "\n")
        os.replace(tmp, self.path)
        self._journal_full = False
        self._dirty.clear()
      except Exception:
        try:
          os.unlink(tmp)
        except Exception:
          pass
    except Exception:
      pass

  # ── write path ──────────────────────────────────────────────────────────

  def _merge_key(self, lat: float, lon: float, bearing: float) -> str | None:
    """The nearest existing record within MERGE_M whose heading agrees, or None.

    Write path only. Probing the 3x3 coarse cells is the same index the read
    path uses, so this costs nothing beyond what a lookup already costs — and
    it happens once per learned corner, not once per frame.
    """
    try:
      cy, cx = coarse_of(lat, lon)
      cos_lat = math.cos(math.radians(lat))
      best_key, best_d = None, MERGE_M
      for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
          for key in self._index.get((cy + dy, cx + dx), ()):
            c = self.corners.get(key)
            if c is None or bearing_delta(c.bearing, bearing) > MERGE_BEARING_DEG:
              continue
            d = math.hypot((c.lat - lat) * 111320.0, (c.lon - lon) * 111320.0 * cos_lat)
            if d < best_d:
              best_key, best_d = key, d
      return best_key
    except Exception:
      return None

  def observe(self, lat: float, lon: float, bearing: float, v: float,
              flags: int = 0, now: float | None = None) -> str:
    """Fold one observation in. Returns the key it landed on.

    The estimate rises fast and falls slow (see ALPHA_UP/ALPHA_DOWN): a cap
    can only slow the car, so learning to brake HARDER is the change that
    deserves more evidence.
    """
    now = time.time() if now is None else now  # noqa: TID251 (persisted across boots; monotonic cannot be)
    key = key_for(lat, lon, bearing)
    if key not in self.corners:
      merged = self._merge_key(lat, lon, bearing)
      if merged is not None:
        key = merged
    c = self.corners.get(key)
    if c is None:
      c = Corner(lat, lon, bearing, v, n=1, t=now, flags=flags)
      self.corners[key] = c
      self._index_add(key, c)
    else:
      alpha = ALPHA_UP if v > c.v else ALPHA_DOWN
      c.v += (v - c.v) * alpha
      c.n += 1
      c.t = now
      c.flags |= flags
    self._dirty.add(key)
    return key

  def maybe_flush(self, now: float) -> bool:
    """Append dirty records. Called every planner frame; does work at most
    once per FLUSH_S and only when there is something to write."""
    if not self._dirty or now - self._last_flush < FLUSH_S:
      return False
    self._last_flush = now
    if self._journal_full:
      self._dirty.clear()
      return False
    try:
      if os.path.getsize(self.path) > MAX_JOURNAL_BYTES:
        # Compaction is a startup job and plannerd restarts every drive, so
        # stopping here costs at most one drive of learning and never risks a
        # multi-megabyte rewrite next to a moving car.
        self._journal_full = True
        self._dirty.clear()
        return False
    except OSError:
      pass

    if not self._have_space():
      self._dirty.clear()
      return False

    try:
      os.makedirs(self.directory, exist_ok=True)
      with open(self.path, "a") as f:
        for key in self._dirty:
          c = self.corners.get(key)
          if c is not None:
            f.write(c.to_json(key) + "\n")
      self._dirty.clear()
      return True
    except Exception:
      self._dirty.clear()
      return False

  # ── read path ───────────────────────────────────────────────────────────

  def nearby(self, lat: float, lon: float, bearing: float,
             max_dist_m: float, bearing_tol_deg: float = 70.0) -> list:
    """Learned corners AHEAD of us, as (distance_m, Corner).

    Probes the 3x3 coarse cells around ego, then filters on distance, heading
    agreement and ahead-ness. "Ahead" is the dot product of our heading with
    the offset to the corner, which is what stops a corner we have just
    exited from capping us on the way out.
    """
    out = []
    try:
      cy, cx = coarse_of(lat, lon)
      cos_lat = math.cos(math.radians(lat))
      hx = math.sin(math.radians(bearing))   # east component of heading
      hy = math.cos(math.radians(bearing))   # north component
      for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
          for key in self._index.get((cy + dy, cx + dx), ()):
            c = self.corners.get(key)
            if c is None:
              continue
            if bearing_delta(c.bearing, bearing) > bearing_tol_deg:
              continue
            north = (c.lat - lat) * 111320.0
            east = (c.lon - lon) * 111320.0 * cos_lat
            d = math.hypot(north, east)
            if d > max_dist_m:
              continue
            if north * hy + east * hx <= 0.0:   # behind us
              continue
            out.append((d, c))
    except Exception:
      return []
    return out

  # ── introspection (dev UI / tests) ──────────────────────────────────────

  @property
  def count(self) -> int:
    return len(self.corners)
