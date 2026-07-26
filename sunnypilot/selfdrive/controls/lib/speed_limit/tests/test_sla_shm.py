"""FunnyPilot v3.4.5 — freshness contract for the SLA /dev/shm channel.

WHY THIS MATTERS MORE THAN IT LOOKS. cruise_ext writes read_sla_shm()'s target
into v_cruise_kph on every 100 Hz control frame while SLA is active. Before
v3.4.5 the reader had no staleness check, so a wedged or dead plannerd left the
last value in the file forever and the car kept obeying a process that no longer
exists — including silently reverting the driver's own SET+ press a fraction of
a second after they made it.

The invariant under test is therefore not "stale reads are nice to reject", it
is: THE FAILURE MODE OF THIS CHANNEL MUST BE 'NO REQUEST', NEVER A STUCK ONE.
That is why the stale cases below assert the value goes to 0.0 even though the
last written value was a perfectly valid, non-zero target.

Stdlib-only; the module is stdlib-only by design so this runs anywhere.
"""
import os
import tempfile
import time

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit import sla_shm


@pytest.fixture(autouse=True)
def tmp_shm(monkeypatch, tmp_path):
  """Point the module at a temp file — /dev/shm is shared with a real device."""
  monkeypatch.setattr(sla_shm, 'SHM_PATH', str(tmp_path / 'fp_sla'))
  yield


def _write_raw(text):
  with open(sla_shm.SHM_PATH, 'w') as f:
    f.write(text)


class TestRoundTrip:
  def test_fresh_value_reads_back(self):
    sla_shm.write_sla_shm(24.5, True)
    v, gate = sla_shm.read_sla_shm()
    assert abs(v - 24.5) < 1e-3
    assert gate is True

  def test_gate_false_round_trips(self):
    sla_shm.write_sla_shm(10.0, False)
    v, gate = sla_shm.read_sla_shm()
    assert abs(v - 10.0) < 1e-3
    assert gate is False

  def test_writer_stamps_three_fields(self):
    sla_shm.write_sla_shm(12.0, True)
    with open(sla_shm.SHM_PATH) as f:
      parts = f.read().strip().split(',')
    assert len(parts) == 3
    assert abs(float(parts[2]) - time.monotonic()) < 1.0


class TestStalenessIsLoadBearing:
  def test_stale_returns_no_request_even_though_the_value_was_valid(self):
    """MUTATION: delete the age check in read_sla_shm.

    This is the whole point. A valid non-zero target sits in the file; only the
    timestamp is old. The reader must still say 'no request', because a live
    cruise set speed being driven by a dead publisher is worse than none.
    """
    stale_stamp = time.monotonic() - (sla_shm.STALE_S + 1.0)
    _write_raw(f"24.500,1,{stale_stamp:.3f}")
    assert sla_shm.read_sla_shm() == (0.0, False)

  def test_legacy_two_field_form_is_stale(self):
    """An old plannerd still running against a new cruise_ext publishes an
    unstamped line. Unstamped == unverifiable == stale, never trusted."""
    _write_raw("24.500,1")
    assert sla_shm.read_sla_shm() == (0.0, False)

  def test_stamp_from_the_future_is_rejected(self):
    _write_raw(f"24.500,1,{time.monotonic() + 60.0:.3f}")
    assert sla_shm.read_sla_shm() == (0.0, False)

  def test_freshness_window_tolerates_missed_writes(self):
    """Writer is 20 Hz; the window must be several frames wide or normal
    scheduling jitter would blink the set speed request off."""
    assert sla_shm.STALE_S >= 5 * 0.05
    _write_raw(f"24.500,1,{time.monotonic() - sla_shm.STALE_S * 0.5:.3f}")
    v, gate = sla_shm.read_sla_shm()
    assert abs(v - 24.5) < 1e-3 and gate is True


class TestGarbageIsNeverFatal:
  @pytest.mark.parametrize("payload", ["", "garbage", ",,", "a,b,c", "24.5,1,notanumber",
                                       "nan,1,%f", "24.5,x,%f"])
  def test_garbage_reads_as_no_request(self, payload):
    _write_raw(payload.replace('%f', f"{time.monotonic():.3f}"))
    v, gate = sla_shm.read_sla_shm()
    assert v == 0.0

  def test_missing_file_reads_as_no_request(self):
    assert not os.path.exists(sla_shm.SHM_PATH)
    assert sla_shm.read_sla_shm() == (0.0, False)

  def test_negative_target_is_rejected(self):
    _write_raw(f"-5.0,0,{time.monotonic():.3f}")
    assert sla_shm.read_sla_shm()[0] == 0.0

  def test_write_to_an_impossible_path_never_raises(self):
    sla_shm.SHM_PATH = '/nonexistent-dir-fp/fp_sla'
    sla_shm.write_sla_shm(1.0, True)  # the assertion is that this returns


class TestNoBannedClock:
  def test_uses_monotonic_not_epoch(self):
    """`time.time` is banned repo-wide by ruff in favour of time.monotonic, and
    an epoch stamp here would be the v3.4.5 resolver bug in a second location.
    Both processes are on one machine, so monotonic is directly comparable."""
    import ast
    import pathlib
    # AST, not a substring scan: the module docstring legitimately NAMES
    # time.time while explaining why it isn't used, and a text matcher would
    # demand that the explanation be deleted.
    tree = ast.parse(pathlib.Path(sla_shm.__file__).read_text())
    attrs = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'time'}
    assert 'time' not in attrs, "epoch clock in a cross-process stamp — see the v3.4.5 resolver bug"
    assert 'monotonic' in attrs


def test_leaves_no_temp_files_behind(tmp_path):
  """The writer uses mkstemp + os.replace for atomicity; a leak would slowly
  fill /dev/shm, which is a RAM-backed tmpfs on the device."""
  before = set(os.listdir(os.path.dirname(sla_shm.SHM_PATH)))
  for i in range(20):
    sla_shm.write_sla_shm(float(i), i % 2 == 0)
  after = set(os.listdir(os.path.dirname(sla_shm.SHM_PATH)))
  assert after - before == {os.path.basename(sla_shm.SHM_PATH)}
  assert tempfile.gettempdir()  # keep the import honest
