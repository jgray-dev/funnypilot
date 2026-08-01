"""FunnyPilot v3.5.0 — SCC cross-process channel over /dev/shm.

WHY A FILE AND NOT CAPNP: the same reason as sla_shm.py — a `cereal/*.capnp`
change forces SCons to regenerate and recompile the schema on the device, and
this fork does not pay a rebuild for telemetry. controlsd already publishes
/dev/shm/lat_interp and plannerd already publishes /dev/shm/fp_sla, so this is
the established pattern rather than a new invention.

WHAT CROSSES, AND WHY IT HAS TO. The onroad minimap draws the route SCC-M is
reasoning about and marks the ONE point it is actually braking for —
`argmin(v_allowed)` inside `scc_map_v2._raw_cap_from_map()`. The UI could
re-derive that from the same `MapTargetVelocities` array, and that is exactly
the trap: two copies of a selection rule drift the moment either is tuned, and
a debug readout that disagrees with the controller is worse than no readout.
So the controller publishes its own answer and the UI draws it.

Format: one line,
"<gov_lat>,<gov_lon>,<gov_v_mps>,<authority 0..1>,<advisory 0|1>,<writer time.monotonic()>"

  gov_lat/lon  the governing point, 0,0 when nothing constrains
  gov_v        the cap SCC-M computed for it, m/s
  authority    what survived scc_fusion, as a fraction of the cut SCC-M asked
               for: 1.0 = vision corroborated and it passed through whole,
               <1.0 = the map is being scaled down, 0.0 = vetoed. This is the
               solid-vs-hollow marker on the minimap and the whole reason the
               channel exists.
  advisory     a posted advisory speed agreed there was something here

STALENESS IS LOAD-BEARING, same as sla_shm: a wedged plannerd must read as "no
constraint", never as a stuck one, or the minimap would keep showing a corner
that the controller stopped thinking about minutes ago. The timestamp is the
writer's `time.monotonic()`, comparable only because both processes share a
machine and a clock (`time.time` is banned repo-wide by ruff anyway).

Readers are best-effort end to end: any failure returns the inactive default.
This is a DIAGNOSTIC channel — nothing in it may ever be able to affect
control, and nothing in the UI may be able to fail because of it.

Import-light (stdlib only).
"""
import os
import tempfile
import time

SHM_PATH = '/dev/shm/fp_scc'
LEARN_SHM_PATH = '/dev/shm/fp_learn'

# Writer is 20 Hz (DT_MDL). 1.0 s is deliberately looser than sla_shm's 0.5 s:
# nothing here touches control, and a marker that blinks out on a single late
# frame would read as a bug in the thing it exists to debug.
STALE_S = 1.0

INACTIVE = (0.0, 0.0, 0.0, 0.0, False)
LEARN_INACTIVE = (0, False, 0.0)


def _atomic_write(path: str, payload: str) -> None:
  """Write via a temp file + os.replace so a reader can never see a torn line.

  Leaving a stray temp file behind on a full /dev/shm would be worse than the
  failed write itself, hence the unlink on the failure path.
  """
  fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.fp_shm')
  try:
    with os.fdopen(fd, 'w') as f:
      f.write(payload)
    os.replace(tmp, path)
  except Exception:
    try:
      os.unlink(tmp)
    except Exception:
      pass


def write_scc_shm(gov_lat: float, gov_lon: float, gov_v: float,
                  authority: float, advisory: bool) -> None:
  """Publish from plannerd. Best-effort; never raises."""
  try:
    _atomic_write(SHM_PATH,
                  f"{float(gov_lat):.7f},{float(gov_lon):.7f},{float(gov_v):.2f}," +
                  f"{float(authority):.3f},{int(bool(advisory))},{time.monotonic():.3f}")
  except Exception:
    pass


def write_learn_shm(count: int, active: bool, confidence: float) -> None:
  """SCC-Learn's state, for the onroad "LRN" pill. Diagnostic only.

  A SEPARATE FILE rather than two more fields on fp_scc, deliberately: the
  minimap reader unpacks fp_scc positionally and its staleness contract is
  pinned by tests. Widening a working channel to carry an unrelated feature is
  how a reader that indexes [4] starts reading a different quantity.
  """
  try:
    _atomic_write(LEARN_SHM_PATH,
                  f"{int(count)},{int(bool(active))},{float(confidence):.3f},{time.monotonic():.3f}")
  except Exception:
    pass


def read_learn_shm() -> tuple[int, bool, float]:
  """(learned_corner_count, governing_now, confidence). LEARN_INACTIVE on any doubt."""
  try:
    with open(LEARN_SHM_PATH) as f:
      parts = f.read().strip().split(',')
    if len(parts) < 4:
      return LEARN_INACTIVE
    age = time.monotonic() - float(parts[3])
    if not -1.0 < age <= STALE_S:
      return LEARN_INACTIVE
    conf = float(parts[2])
    if conf != conf:  # NaN
      return LEARN_INACTIVE
    return int(parts[0]), bool(int(parts[1])), min(max(conf, 0.0), 1.0)
  except Exception:
    return LEARN_INACTIVE


def read_scc_shm() -> tuple[float, float, float, float, bool]:
  """Read from the UI. Returns (gov_lat, gov_lon, gov_v, authority, advisory).

  INACTIVE (all zero / False) means "nothing to draw", which is the safe
  default for every failure mode: missing file, torn read, garbage, stale
  writer, or a plannerd that never started.
  """
  try:
    with open(SHM_PATH) as f:
      parts = f.read().strip().split(',')
    if len(parts) < 6:
      return INACTIVE
    age = time.monotonic() - float(parts[5])
    if not -1.0 < age <= STALE_S:  # a stamp from the future, and NaN, land here too
      return INACTIVE
    lat, lon, v, auth = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    if any(x != x for x in (lat, lon, v, auth)):  # NaN
      return INACTIVE
    return lat, lon, v, min(max(auth, 0.0), 1.0), bool(int(parts[4]))
  except Exception:
    return INACTIVE
