"""FunnyPilot v3.4.1 — SLA cross-process channel over /dev/shm.

WHY A FILE AND NOT CAPNP: v3.4.0 added four fields to cereal/custom.capnp to
carry the SLA set-speed ramp target and gas-gate flag between processes. A
.capnp change forces SCons to regenerate and recompile the schema on the
device, which is a real risk on a car that has to boot. Everything else these
features touch is pure Python (plannerd, card) or Python + raylib (the UI), so
nothing needs to be compiled — and a small /dev/shm file gets the same two
values across the process boundary with zero build impact. controlsd already
does exactly this with /dev/shm/lat_interp, so this is the fork's established
pattern, not a new invention.

CORRECTION (v3.4.3): the capnp change was NOT what bricked v3.4.0. That was
originally suspected and it was wrong — the device log shows no build error at
all. The actual cause was a single `car.CarState | None` parameter annotation
in cruise_ext.py raising TypeError at import time and killing manager (see
sunnypilot/tests/test_capnp_annotations.py). This module stays because
avoiding schema changes is still the right call on its own merits, not because
capnp caused the outage. Do not let the corrected history erode the rule.

Format (v3.4.5): a single line,
"<v_cruise_target_mps>,<gas_gate 0|1>,<writer time.monotonic()>",
rewritten by plannerd at 20 Hz. Readers are best-effort: any failure (missing
file, torn read, garbage) returns the inactive default and the caller carries
on, so a telemetry problem can never take down control.

STALENESS IS LOAD-BEARING (v3.4.5). Before this, the reader had no freshness
check at all: if plannerd died or wedged, /dev/shm/fp_sla kept its last value
forever and cruise_ext went on writing it to v_cruise_kph every 100 Hz frame.
A driver pressing SET+ would then see their own adjustment silently reverted a
fraction of a second later, by a process that is no longer running. The whole
point of this channel being "best-effort" is that its failure mode must be
NO REQUEST, not a stuck one.

The timestamp is the WRITER's time.monotonic(), which is only comparable
because both processes are on the same machine and the same clock. (`time.time`
is banned repo-wide by ruff in favour of time.monotonic, so an epoch stamp was
never an option here — and would have been the v3.4.5 resolver bug all over
again.) The legacy 2-field form carries no stamp and is therefore treated as
stale, so a running old plannerd cannot feed a new cruise_ext.
"""
import os
import tempfile
import time

SHM_PATH = '/dev/shm/fp_sla'

# Writer is 20 Hz (DT_MDL). 0.5 s = 10 missed writes: long enough that normal
# scheduling jitter never trips it, short enough that a wedged publisher cannot
# fight a driver button press for more than a blink.
STALE_S = 0.5


def write_sla_shm(v_cruise_target: float, gas_gate_active: bool) -> None:
  """Publish from plannerd. Best-effort; never raises."""
  try:
    payload = f"{float(v_cruise_target):.3f},{int(bool(gas_gate_active))},{time.monotonic():.3f}"
    # atomic replace so a reader can never observe a half-written line
    d = os.path.dirname(SHM_PATH)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.fp_sla')
    try:
      with os.fdopen(fd, 'w') as f:
        f.write(payload)
      os.replace(tmp, SHM_PATH)
    except Exception:
      try:
        os.unlink(tmp)
      except Exception:
        pass
      raise
  except Exception:
    pass


def read_sla_shm() -> tuple[float, bool]:
  """Read from card / the UI. Returns (v_cruise_target_mps, gas_gate_active);
  (0.0, False) means 'no request', which is the safe inactive default."""
  try:
    with open(SHM_PATH) as f:
      parts = f.read().strip().split(',')
    if len(parts) < 3:
      return 0.0, False  # legacy 2-field form: unstamped == unverifiable == stale
    age = time.monotonic() - float(parts[2])
    if not -1.0 < age <= STALE_S:  # also rejects a stamp from the future (NaN falls out here too)
      return 0.0, False
    target = float(parts[0])
    gate = bool(int(parts[1]))
    if target != target or target < 0.0:  # NaN / nonsense
      return 0.0, gate
    return target, gate
  except Exception:
    return 0.0, False
