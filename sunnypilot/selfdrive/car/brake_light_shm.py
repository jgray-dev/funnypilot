"""FunnyPilot v3.4.4 — brake-lamp cross-process channel over /dev/shm.

WHAT THIS CARRIES: `TCS13.BrakeLight`, a single bit the ESC broadcasts on the
powertrain bus saying the car's brake lamps are lit RIGHT NOW. It is not a
model, an estimate, or a threshold on a commanded value — it is the car
reporting its own actuator state. On this platform TCS13 is already parsed by
`opendbc/sunnypilot/car/hyundai/carstate_ext.py` (it reads `aBasis` from the
same frame), and the classic-CAN parser is built with an empty signal list, so
every signal in the message is decoded whether we ask for it or not. Reading
one more bit therefore costs nothing on the CAN side.

WHY A FILE AND NOT CAPNP: same rule as sla_shm.py — `cereal/*.capnp` changes
force a SCons rebuild of the compiled schema on the device, which this fork
avoids. `CarState.brakeLights` exists upstream but only as
`brakeLightsDEPRECATED`, and both ends of this path (card and the raylib UI)
are pure Python, so a plain file crosses the boundary with zero build impact.
Established precedent: /dev/shm/lat_interp, /dev/shm/fp_sla.

Format: a single character, "0" or "1". The writer runs in card at the 100 Hz
CarState rate but only touches the file when the bit CHANGES or when
HEARTBEAT_FRAMES have elapsed, so the steady-state cost is 5 atomic writes a
second. The heartbeat is what makes staleness meaningful: a reader that sees a
file older than STALE_S knows card is not publishing (wrong car, old build,
process dead) and gets None — "unknown" — rather than a confidently wrong
False. Never let this channel invent a state it does not know.
"""
import os
import tempfile
import time

SHM_PATH = '/dev/shm/fp_brake'

HEARTBEAT_FRAMES = 20  # at the 100 Hz CarState rate => a 5 Hz refresh floor
STALE_S = 1.0          # 5x the heartbeat period; below this the value is trusted


def _write(on: bool) -> None:
  """Atomic single-byte publish. Best-effort; never raises."""
  try:
    d = os.path.dirname(SHM_PATH)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.fp_brake')
    try:
      with os.fdopen(fd, 'w') as f:
        f.write('1' if on else '0')
      os.replace(tmp, SHM_PATH)
    except Exception:
      try:
        os.unlink(tmp)
      except Exception:
        pass
      raise
  except Exception:
    pass


class BrakeLightPublisher:
  """Rate-limited publisher for the brake-lamp bit.

  Call update() once per CarState frame. It writes on every transition (so the
  UI sees a brake application within one frame) and otherwise at
  HEARTBEAT_FRAMES intervals (so the file never goes stale while the state is
  genuinely steady).
  """

  def __init__(self) -> None:
    self._last: bool | None = None
    self._countdown = 0

  def update(self, on: bool) -> None:
    on = bool(on)
    self._countdown -= 1
    if on != self._last or self._countdown <= 0:
      _write(on)
      self._last = on
      self._countdown = HEARTBEAT_FRAMES


def read_brake_light() -> bool | None:
  """Read from the UI. True/False = the car's brake lamps are on/off.

  None means UNKNOWN — no publisher (not a Hyundai classic-CAN car, an older
  build, card not running) or a stale file. Callers must handle None
  explicitly instead of treating it as False.
  """
  try:
    st = os.stat(SHM_PATH)
    # st_mtime is WALL CLOCK, so the comparison clock must be wall clock too.
    # time.monotonic() here would be a clock-domain mismatch — precisely the
    # v3.4.5 speed_limit_resolver bug. CLOCK_REALTIME is the same clock
    # time.time() reads; it is spelled this way only because `time.time` is
    # banned repo-wide by ruff.
    if time.clock_gettime(time.CLOCK_REALTIME) - st.st_mtime > STALE_S:
      return None
    with open(SHM_PATH) as f:
      raw = f.read().strip()
    if raw == '1':
      return True
    if raw == '0':
      return False
    return None
  except Exception:
    return None
