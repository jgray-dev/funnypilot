"""FunnyPilot v3.6.2 — the learned-corner store: our own map, built by driving.

THE IDEA. SCC-M v2 measures a bend's radius from the road (road_geometry.py)
and picks a speed with `v = sqrt(a_lat * R)`. What it cannot measure from a
polyline is `a_lat`: how hard THIS car, on THIS surface, with THIS camber, can
actually corner without oscillating or running the steering out of authority.
That is what this file remembers, keyed by position and heading.

WHAT CHANGED IN v3.6.2, AND WHY THE FILE NAME CHANGED WITH IT. Until v3.6.1
each record held a SPEED: the minimum of a speed dip the driver or SCC-V had
produced. That is a different quantity from the one stored now — it describes
what happened rather than what the corner supports, and it is only valid at the
radius it was measured at. Merging the two would have quietly poisoned every
estimate, so the store starts a new journal (`corners_v2.jsonl`) and the old
one is simply left on disk, harmless and unread.

Each record now carries an INTERVAL, `a_lo` and `a_hi`:

    a_lo  the largest lateral acceleration a clean pass has demonstrated here
    a_hi  the smallest that a stressed pass has shown to be too much

A pass closes the interval from one side or neither; see corner_speed.py for
the update rule and the reason its asymmetry runs the opposite way to the old
store's. `r` is the radius the corner was measured at when last seen, kept for
diagnostics and so a record can be sanity-checked against fresh geometry.

v3.6.2 adds `d`, the mean movement of the learned value per visit:

    d     how far each pass is still shifting min(a_lo, a_hi), m/s^2

It exists because visit count turned out to be the wrong measure of
confidence. A corner seen three times whose answer is still swinging is not
known; a corner seen three hundred times whose answer never moves is. `d`
is the difference between those two, and it is what corner_speed.confidence_of
multiplies the visit count by. A journal line written before v3.6.2 has no
`d` and reads as UNSETTLED — see Corner.from_obj for why that direction.

WHAT THIS MODULE IS: only the persistence and the geometry index. The decision
about WHEN something is worth recording lives in scc_map_v2.py, and the
decision about how much authority a learned point gets lives in scc_fusion.py.
Keeping those apart is what makes the hard parts testable without a car.

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

Import-light: stdlib plus corner_speed, which is itself stdlib-only. Nothing
here reaches cereal, numpy or params, so it tests without a car.
"""
import json
import math
import os
import tempfile
import threading
import time

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.corner_speed import (
  A_LAT_DEFAULT, A_LAT_MAX, DRIFT_UNKNOWN, update_drift, update_interval)

STORE_DIR = "/data/funnypilot_scc_learn"
# v3.6.2 — a NEW file, not a migration. See the module docstring: the old
# records held a speed with different semantics, and a converted record would
# be indistinguishable from a measured one while being wrong.
STORE_NAME = "corners_v2.jsonl"

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
# v3.5.1: 47, not 60. loggerd rotates a segment every 60 s, so a 60 s flush
# period beats against it and lands on a busy eMMC every time the two phases
# coincide — which is a good description of "it happened once on that drive".
# The writer thread below makes this a non-issue either way; the odd period is
# belt and braces, and free.
FLUSH_S = 47.0                # batch dirty records; a long drive writes a few tens of KB

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
  """One learned bend: where it is, which way it is taken, the lateral
  acceleration interval this car has demonstrated through it, and how much
  that interval is still moving.

  `d` (v3.6.2) is the mean absolute movement of the learned value per visit,
  m/s^2. It is what separates "we have been here three times" from "we know
  this corner" — see corner_speed.confidence_of.
  """
  __slots__ = ("lat", "lon", "bearing", "a_lo", "a_hi", "r", "n", "t", "flags", "d")

  def __init__(self, lat, lon, bearing, a_lo, a_hi, r=0.0, n=1, t=0.0, flags=0,
               d=DRIFT_UNKNOWN):
    self.lat, self.lon, self.bearing = lat, lon, bearing
    self.a_lo, self.a_hi, self.r = a_lo, a_hi, r
    self.n, self.t, self.flags = n, t, flags
    self.d = d

  def to_json(self, key: str) -> str:
    return json.dumps({"k": key, "la": round(self.lat, 6), "lo": round(self.lon, 6),
                       "br": round(self.bearing, 1),
                       "alo": round(self.a_lo, 3), "ahi": round(self.a_hi, 3),
                       "r": round(self.r, 1),
                       "n": self.n, "t": round(self.t, 0), "f": self.flags,
                       # 3 dp is 0.001 m/s^2, under a percent of the span
                       # between DRIFT_SETTLED and DRIFT_LEARNING, and keeps
                       # the record inside its size budget. See
                       # test_store_json_is_compact.
                       "d": round(self.d, 3)},
                      separators=(",", ":"))

  @staticmethod
  def from_obj(d):
    # A record missing `alo`/`ahi` is not one of ours. Raising here is correct:
    # `load()` skips lines it cannot parse, so a stray old-format journal costs
    # nothing rather than being silently reinterpreted as an acceleration.
    #
    # `d` DEFAULTS TO UNSETTLED, NOT TO SETTLED. A the first cut of v3.6.2 line carries no
    # convergence history, and the safe reading of "we do not know whether this
    # corner has converged" is that it has not — that costs the record its
    # earned speed until it re-proves itself, where the other default would
    # hand full authority to a number with no evidence of stability behind it.
    return d["k"], Corner(float(d["la"]), float(d["lo"]), float(d["br"]),
                          float(d["alo"]), float(d["ahi"]),
                          float(d.get("r", 0.0)), int(d.get("n", 1)),
                          float(d.get("t", 0.0)), int(d.get("f", 0)),
                          float(d.get("d", DRIFT_UNKNOWN)))


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
    # Tracked rather than stat()ed: `maybe_flush` runs in plannerd's 20 Hz loop
    # and must make no syscalls at all (see the writer block).
    self._journal_bytes = 0
    self._writer: threading.Thread | None = None
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
    # rewrite so the journal starts each drive at its compacted size. The READ
    # above is synchronous because the planner needs the data; the WRITE is
    # handed to the writer thread, because it is the biggest one we ever make.
    self._journal_bytes = sum(len(c.to_json(k)) + 1 for k, c in self.corners.items())
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

  # ── writing, which does NOT happen on the caller's thread ────────────────
  #
  # THE v3.5.1 FIX, and the rule behind it: NOTHING IN plannerd's 20 Hz LOOP
  # MAY TOUCH /data. selfdrived marks a service dead after 10 missed frames —
  # 0.5 s for a 20 Hz publisher — and raises `commIssue`, which is a
  # SOFT_DISABLE: a full-screen orange "TAKE CONTROL IMMEDIATELY" for a car
  # that is in fact still driving perfectly, because the planner simply
  # returned late and then caught up. A single append to a busy eMMC (loggerd
  # is writing megabytes beside us, and `statvfs`/`makedirs`/`open` can all
  # block on it) is enough.
  #
  # THE SHAPE IS DELIBERATE — a SHORT-LIVED thread per flush, handed a
  # finished list of lines, not a long-lived worker with a queue:
  #   * it owns no shared mutable state, so there is no lock and no race with
  #     the planner mutating `corners` underneath it;
  #   * at most one exists at a time (`_writer` is checked before spawning), so
  #     a stalled disk cannot pile threads up;
  #   * it holds one bounded list and then dies, which satisfies the v3.4.6
  #     rule that anything running beside a moving car is bounded in MEMORY,
  #     not merely in time.
  # A dropped flush costs at most one drive of learning, which is the same
  # price every other failure path here pays.

  def _writer_busy(self) -> bool:
    w = self._writer
    return w is not None and w.is_alive()

  def _spawn_write(self, lines: list, append: bool) -> bool:
    """Hand a finished payload to a short-lived daemon thread. Never blocks."""
    if self._writer_busy():
      return False
    try:
      self._writer = threading.Thread(target=self._write_blocking, args=(lines, append),
                                      name="scc_learn_store", daemon=True)
      self._writer.start()
      return True
    except Exception:
      self._writer = None
      return False

  def _write_blocking(self, lines: list, append: bool) -> None:
    """Runs on the writer thread ONLY. Every path is best-effort."""
    try:
      os.makedirs(self.directory, exist_ok=True)
      if not self._have_space():
        return
      if append:
        with open(self.path, "a") as f:
          f.writelines(lines)
        return
      fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".corners")
      try:
        with os.fdopen(fd, "w") as f:
          f.writelines(lines)
        os.replace(tmp, self.path)
      except Exception:
        try:
          os.unlink(tmp)
        except Exception:
          pass
    except Exception:
      pass

  def _rewrite(self) -> None:
    """Compacted full dump. Startup only, and off-thread like everything else —
    this one is the biggest write the module ever makes."""
    self._journal_full = False
    self._dirty.clear()
    self._spawn_write([c.to_json(key) + "\n" for key, c in self.corners.items()], append=False)

  def join_writes(self, timeout: float = 2.0) -> None:
    """Tests only: wait for the writer so the file can be asserted on."""
    w = self._writer
    if w is not None:
      w.join(timeout)

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

  def observe(self, lat: float, lon: float, bearing: float, radius: float,
              a_peak: float, severity: float, flags: int = 0,
              now: float | None = None, allow_raise: bool = True) -> str:
    """Fold one traversal in. Returns the key it landed on.

    The interval maths lives in corner_speed.update_interval — this method owns
    only where the record goes and how the visit is counted. Keeping the rule
    out of the persistence layer is what lets it be mutation-tested without
    touching a filesystem.

    `allow_raise=False` records the visit but refuses to move the FLOOR up. The
    caller passes it when SCC-M v2 was itself governing the pass: a corner we
    held the car back through cannot be evidence that the corner is fast. Note
    it does NOT block the ceiling — a governed pass that still oscillated is
    real evidence in the safe direction, and refusing it would mean the one
    situation where we are demonstrably wrong is the one we never learn from.
    """
    now = time.time() if now is None else now  # noqa: TID251 (persisted across boots; monotonic cannot be)
    key = key_for(lat, lon, bearing)
    if key not in self.corners:
      merged = self._merge_key(lat, lon, bearing)
      if merged is not None:
        key = merged
    c = self.corners.get(key)
    fresh = c is None
    if fresh:
      c = Corner(lat, lon, bearing, A_LAT_DEFAULT, A_LAT_MAX, radius, n=0, t=now, flags=0)
      self.corners[key] = c
      self._index_add(key, c)

    before = min(c.a_lo, c.a_hi)
    lo, hi = update_interval(c.a_lo, c.a_hi, a_peak, severity, seed=fresh)
    if not allow_raise:
      lo = min(lo, c.a_lo)
    c.a_lo, c.a_hi = lo, hi
    # v3.6.2 — how far this pass moved the answer, which is what makes the
    # difference between a corner we have visited and one we have worked out.
    #
    # MEASURED ON `min(a_lo, a_hi)`, THE RAW LEARNED VALUE, NOT ON
    # `effective_a_lat`. The effective value already contains the confidence
    # weight, and confidence is about to be computed FROM this drift — feeding
    # one into the other closes a loop in which a corner that lost confidence
    # would appear to move less, regain confidence, move more, and oscillate.
    # The raw interval has no such dependency.
    c.d = update_drift(c.d, min(lo, hi) - before)
    if radius > 0.0:
      c.r = radius
    c.n += 1
    c.t = now
    c.flags |= flags
    self._dirty.add(key)
    # Bounded in MEMORY, not merely on disk. `load()` evicts once at startup,
    # which in practice is enough -- reaching MAX_RECORDS inside a single drive
    # would take 25,000 committed corners. "In practice" is not a bound though,
    # and the rule this module is written under (v3.4.6) is that anything
    # running beside a moving car must be bounded by construction.
    if len(self.corners) > self.max_records:
      self._evict()
      self._reindex()
      self._dirty &= self.corners.keys()
    return key

  def maybe_flush(self, now: float) -> bool:
    """Hand dirty records to the writer thread. Called every planner frame.

    DOES NO IO ITSELF — see the writer block above. Everything on this path is
    dict/str work in memory; the only syscall-shaped thing left is the journal
    size check, and that is a cached `_journal_bytes` estimate rather than a
    `stat`, for the same reason.
    """
    if not self._dirty or now - self._last_flush < FLUSH_S:
      return False
    self._last_flush = now
    if self._journal_full or self._writer_busy():
      return False
    if self._journal_bytes > MAX_JOURNAL_BYTES:
      # Compaction is a startup job and plannerd restarts every drive, so
      # stopping here costs at most one drive of learning and never risks a
      # multi-megabyte rewrite next to a moving car.
      self._journal_full = True
      self._dirty.clear()
      return False

    lines = [c.to_json(key) + "\n" for key in self._dirty
             if (c := self.corners.get(key)) is not None]
    self._dirty.clear()
    if not lines:
      return False
    self._journal_bytes += sum(len(ln) for ln in lines)
    return self._spawn_write(lines, append=True)

  # ── read path ───────────────────────────────────────────────────────────

  def nearby(self, lat: float, lon: float, bearing: float,
             max_dist_m: float, bearing_tol_deg: float = 70.0,
             ahead_only: bool = True) -> list:
    """Learned corners near a point, as (distance_m, Corner).

    Probes the 3x3 coarse cells around the query point, then filters on
    distance and heading agreement. With `ahead_only` (the default) it also
    requires the corner to be in front, via the dot product of the heading with
    the offset — that is what stops a bend you have just exited from capping
    you on the way out, when the query point is EGO.

    `ahead_only=False` IS NOT AN OPTIMISATION, IT IS A DIFFERENT QUESTION.
    SCC-M v2 asks "what do we know about THIS corner" with the corner's own
    position, where the offset is zero, the dot product is zero, and an
    ahead-only probe rejects the exact record it was looking for — so nothing
    learned would ever be used, while every other test still passed. Ahead-ness
    is already established by the geometry that produced the corner; asking for
    it again here is asking a point whether it is in front of itself.
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
            if ahead_only and north * hy + east * hx <= 0.0:   # behind us
              continue
            out.append((d, c))
    except Exception:
      return []
    return out

  # ── introspection (dev UI / tests) ──────────────────────────────────────

  @property
  def count(self) -> int:
    return len(self.corners)
