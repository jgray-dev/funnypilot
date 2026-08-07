"""FunnyPilot v3.6.2 — the /dev/shm channels between plannerd and the UI.

THE ONE PROPERTY THAT MATTERS ON ALL OF THEM: a wedged writer must read as
"nothing", never as a stuck value. The minimap marking a corner the controller
stopped thinking about minutes ago is worse than no marker, and a tinted
ribbon showing speeds from a dead planner is worse than a plain one.

These are diagnostic channels. Nothing in them may affect control, and nothing
in the UI may be able to fail because of them — so every reader's failure path
is tested, not just its happy path.
"""
import time

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm


class Corner:
  """The attributes write_corners_shm needs, i.e. TrackedCorner's shape.

  v3.6.2 — `settled` replaces `confidence` here, and `visits` is new. Those
  are two different questions (has the answer converged / have we been here
  at all) and the minimap needs both; see SCCMapV2._lookup.
  """
  def __init__(self, lat, lon, half_len, v_target, settled, visits=0):
    self.lat, self.lon = lat, lon
    self.half_len, self.v_target = half_len, v_target
    self.settled, self.visits = settled, visits


@pytest.fixture
def paths(tmp_path, monkeypatch):
  monkeypatch.setattr(scc_shm, "SHM_PATH", str(tmp_path / "fp_scc"))
  monkeypatch.setattr(scc_shm, "CORNERS_SHM_PATH", str(tmp_path / "fp_corners"))
  monkeypatch.setattr(scc_shm, "LAT_INTERP_PATH", str(tmp_path / "lat_interp"))
  monkeypatch.setattr(scc_shm, "DEBUG_SHM_PATH", str(tmp_path / "fp_sccdbg"))
  return tmp_path


class TestTheGoverningCorner:
  def test_round_trip(self, paths):
    scc_shm.write_scc_shm(35.123456, -78.987654, 17.9, 1.0, True)
    lat, lon, v, auth, learned = scc_shm.read_scc_shm()
    assert abs(lat - 35.123456) < 1e-5
    assert abs(lon + 78.987654) < 1e-5
    assert abs(v - 17.9) < 1e-2
    assert auth == 1.0 and learned is True

  def test_missing_file_is_inactive(self, paths):
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_garbage_is_inactive(self, paths):
    (paths / "fp_scc").write_text("not,a,valid,line")
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_stale_is_inactive_even_with_a_good_payload(self, paths):
    """MUTATION: drop the freshness check. A wedged plannerd would leave the
    minimap marking a corner it stopped thinking about minutes ago."""
    old = time.monotonic() - (scc_shm.STALE_S + 1.0)
    (paths / "fp_scc").write_text(f"35.0,-78.0,17.9,1.0,1,{old:.3f}")
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_a_stamp_from_the_future_is_rejected(self, paths):
    (paths / "fp_scc").write_text(f"35.0,-78.0,17.9,1.0,1,{time.monotonic() + 60:.3f}")
    assert scc_shm.read_scc_shm() == scc_shm.INACTIVE

  def test_authority_is_clamped(self, paths):
    (paths / "fp_scc").write_text(f"35.0,-78.0,17.9,7.5,0,{time.monotonic():.3f}")
    assert scc_shm.read_scc_shm()[3] == 1.0

  def test_writing_to_a_bad_path_never_raises(self, monkeypatch):
    """This runs inside plannerd. A telemetry problem may not touch control."""
    monkeypatch.setattr(scc_shm, "SHM_PATH", "/nonexistent-dir/fp_scc")
    scc_shm.write_scc_shm(1.0, 2.0, 3.0, 1.0, False)


class TestTheCornerList:
  """v3.6.2. The minimap tints the road by the speed SCC-M v2 chose for each
  corner, and it CANNOT compute those itself: half of each one comes from a
  store on /data, and nothing in the onroad HUD may touch a filesystem."""

  def test_round_trip(self, paths):
    scc_shm.write_corners_shm([Corner(35.1, -78.2, 40.0, 12.5, 0.45, visits=2),
                               Corner(35.2, -78.3, 90.0, 18.0, 1.0, visits=9)])
    out = scc_shm.read_corners_shm()
    assert len(out) == 2
    assert out[0][0] == pytest.approx(35.1, abs=1e-5)
    assert out[0][3] == pytest.approx(12.5, abs=0.01)
    assert out[1][4] == pytest.approx(1.0, abs=0.01)
    assert out[0][5] == 2 and out[1][5] == 9

  def test_a_five_field_line_still_tints_but_claims_no_visits(self, paths):
    """v3.6.2 widened the entry from 5 fields to 6. A 5-field line — an older
    writer, or a torn upgrade — must still yield its SPEED, which is what the
    tint needs and is always valid, while reporting 0 visits so no learned
    ring is drawn for a corner the line cannot vouch for.

    MUTATION: require 6 fields and the ribbon loses its tint entirely on any
    version skew, which is a far louder failure than a missing ring."""
    (paths / "fp_corners").write_text(
      f"{time.monotonic():.3f};35.1,-78.2,40,12.5,0.45")
    out = scc_shm.read_corners_shm()
    assert len(out) == 1
    assert out[0][3] == pytest.approx(12.5, abs=0.01)
    assert out[0][5] == 0

  def test_a_garbage_visit_count_is_zero_not_fatal(self, paths):
    """The visit count is the only integer on this line, so it is the only
    field a bad write can make unparseable without also killing the floats."""
    (paths / "fp_corners").write_text(
      f"{time.monotonic():.3f};35.1,-78.2,40,12.5,0.45,banana")
    out = scc_shm.read_corners_shm()
    assert len(out) == 1 and out[0][5] == 0

  def test_a_negative_visit_count_reads_as_none(self, paths):
    """Nothing can be visited -3 times; clamping rather than trusting keeps a
    corrupt line from producing a ring with a nonsensical opacity."""
    (paths / "fp_corners").write_text(
      f"{time.monotonic():.3f};35.1,-78.2,40,12.5,0.45,-3")
    assert scc_shm.read_corners_shm()[0][5] == 0

  def test_an_empty_list_round_trips(self, paths):
    scc_shm.write_corners_shm([])
    assert scc_shm.read_corners_shm() == []

  def test_missing_file_is_empty(self, paths):
    assert scc_shm.read_corners_shm() == []

  def test_garbage_is_empty(self, paths):
    (paths / "fp_corners").write_text("]{ nonsense ;;;;")
    assert scc_shm.read_corners_shm() == []

  def test_stale_is_empty(self, paths):
    """A tinted ribbon showing a dead planner's speeds is worse than a plain
    one: it looks exactly like a live one."""
    old = time.monotonic() - (scc_shm.STALE_S + 1.0)
    (paths / "fp_corners").write_text(f"{old:.3f};35.1,-78.2,40,12.5,0.45")
    assert scc_shm.read_corners_shm() == []

  def test_a_partial_entry_is_dropped_not_fatal(self, paths):
    (paths / "fp_corners").write_text(
      f"{time.monotonic():.3f};35.1,-78.2;35.2,-78.3,90,18.0,1.0")
    out = scc_shm.read_corners_shm()
    assert len(out) == 1

  def test_the_payload_is_bounded(self, paths):
    """A 20 Hz write whose size depends on how curvy the road is would be a
    cost that only appears where it is hardest to debug."""
    scc_shm.write_corners_shm([Corner(35.0 + i * 0.001, -78.0, 40.0, 12.0, 0.5,
                                      visits=12345)
                               for i in range(200)])
    assert len(scc_shm.read_corners_shm()) <= scc_shm.MAX_CORNERS
    assert len((paths / "fp_corners").read_text()) < 1200

  def test_writing_garbage_never_raises(self, paths):
    scc_shm.write_corners_shm(None)
    scc_shm.write_corners_shm([object()])
    scc_shm.write_corners_shm([Corner(float('nan'), 0.0, 0.0, 0.0, 0.0)])

  def test_nan_entries_are_dropped_on_read(self, paths):
    (paths / "fp_corners").write_text(
      f"{time.monotonic():.3f};nan,-78.2,40,12.5,0.45;35.2,-78.3,90,18.0,1.0")
    assert len(scc_shm.read_corners_shm()) == 1


class TestTheEpsLimitedReader:
  """Reads controlsd's existing lat_interp heartbeat rather than adding a
  channel. That file carries no timestamp, which is why the failure default is
  False: an unreadable file contributes NO stress, so a dead reader can only
  make a corner look cleaner than it was, never worse."""

  def test_a_biting_clamp_reads_true(self, paths):
    (paths / "lat_interp").write_text("5,0.80,3.2,0.40,0.010")
    assert scc_shm.read_eps_limited() is True

  def test_an_idle_clamp_reads_false(self, paths):
    (paths / "lat_interp").write_text("5,1.00,1.1,0.00,0.000")
    assert scc_shm.read_eps_limited() is False

  def test_missing_or_garbage_reads_false(self, paths):
    assert scc_shm.read_eps_limited() is False
    (paths / "lat_interp").write_text("junk")
    assert scc_shm.read_eps_limited() is False
    (paths / "lat_interp").write_text("5,1.00")
    assert scc_shm.read_eps_limited() is False

  def test_an_older_shorter_heartbeat_reads_false(self, paths):
    """v3.4.9 appended the fifth field. A reader that indexed past the end
    would raise inside plannerd's 100 Hz loop."""
    (paths / "lat_interp").write_text("5,1.00,1.1")
    assert scc_shm.read_eps_limited() is False


class TestTheLearnPill:
  def test_round_trip(self, tmp_path, monkeypatch):
    monkeypatch.setattr(scc_shm, "LEARN_SHM_PATH", str(tmp_path / "fp_learn"))
    scc_shm.write_learn_shm(42, True, 0.75)
    assert scc_shm.read_learn_shm() == (42, True, 0.75)

  def test_stale_is_inactive(self, tmp_path, monkeypatch):
    p = tmp_path / "fp_learn"
    monkeypatch.setattr(scc_shm, "LEARN_SHM_PATH", str(p))
    p.write_text(f"42,1,0.750,{time.monotonic() - scc_shm.STALE_S - 1:.3f}")
    assert scc_shm.read_learn_shm() == scc_shm.LEARN_INACTIVE


class TestTheDevUiChannel:
  """v3.6.2. One line carrying everything the dev UI needs to watch SCC-M v2
  on its first drives. Same staleness contract as every other channel here:
  a dev readout showing a dead planner's last numbers as if they were live is
  worse than one showing zeros, because it looks exactly like a working one."""

  ROW = (3, 118.0, 14.5, 210.0, 1.95, 2, 1, 16.2, 0.75, 41, 2.4, 0.3, 118.0, 7)

  def test_round_trip(self, paths):
    scc_shm.write_scc_debug_shm(self.ROW)
    out = scc_shm.read_scc_debug_shm()
    assert out[0] == 3 and out[5] == 2 and out[9] == 41 and out[13] == 7
    assert out[6] is True
    assert out[1] == pytest.approx(118.0, abs=0.01)
    assert out[8] == pytest.approx(0.75, abs=0.01)

  def test_missing_stale_and_garbage_all_read_inactive(self, paths):
    assert scc_shm.read_scc_debug_shm() == scc_shm.DEBUG_INACTIVE
    (paths / "fp_sccdbg").write_text("nonsense")
    assert scc_shm.read_scc_debug_shm() == scc_shm.DEBUG_INACTIVE
    old = time.monotonic() - (scc_shm.STALE_S + 1.0)
    (paths / "fp_sccdbg").write_text(",".join(["1.0"] * 14) + f",{old:.3f}")
    assert scc_shm.read_scc_debug_shm() == scc_shm.DEBUG_INACTIVE

  def test_a_short_row_is_rejected_rather_than_indexed_past(self, paths):
    """This is read from the UI process. An IndexError here is a dead HUD."""
    (paths / "fp_sccdbg").write_text(f"1.0,2.0,3.0,{time.monotonic():.3f}")
    assert scc_shm.read_scc_debug_shm() == scc_shm.DEBUG_INACTIVE

  def test_writing_garbage_never_raises(self, paths):
    scc_shm.write_scc_debug_shm(None)
    scc_shm.write_scc_debug_shm(["x"])
    scc_shm.write_scc_debug_shm([float('nan')] * 14)


class TestThePitchRateReader:
  """v3.6.2. corner_effort needs to know when the ROAD is hitting the car, so
  a bump-induced correction is not mistaken for a corner taken too fast. The
  signal already existed as field 2 of controlsd's lat_interp heartbeat and
  was read by nothing but the dev UI.

  THE FAILURE DIRECTION IS THE WHOLE CONTRACT: 0.0 means NOT disturbed, so an
  unreadable file leaves every pass measured exactly as it was before this
  existed. Failing toward 'disturbed' would silently suppress the measurements
  the entire learning path depends on -- a far quieter and worse failure than
  the confound it guards against.
  """

  def test_it_reads_field_two(self, paths):
    (paths / "lat_interp").write_text("5,1.00,7.3,0.00,0.012")
    assert scc_shm.read_pitch_rate() == pytest.approx(7.3)

  def test_it_is_a_magnitude(self, paths):
    """Pitching nose-down and nose-up are the same disturbance."""
    (paths / "lat_interp").write_text("5,1.00,-7.3,0.00,0.012")
    assert scc_shm.read_pitch_rate() == pytest.approx(7.3)

  def test_a_missing_file_is_not_disturbed(self, paths):
    """MUTATION: return a non-zero default. Every corner would then read as
    permanently bumpy and nothing would ever be learned."""
    assert scc_shm.read_pitch_rate() == 0.0

  def test_garbage_is_not_disturbed(self, paths):
    for junk in ("", "nonsense", "5", "5,1.00", "5,1.00,banana,0,0"):
      (paths / "lat_interp").write_text(junk)
      assert scc_shm.read_pitch_rate() == 0.0

  def test_nan_and_inf_are_not_disturbed(self, paths):
    for junk in ("5,1.00,nan,0.00,0.0", "5,1.00,inf,0.00,0.0"):
      (paths / "lat_interp").write_text(junk)
      assert scc_shm.read_pitch_rate() == 0.0

  def test_it_does_not_disturb_the_eps_limited_reader(self, paths):
    """Both read the same line. Field 3 is `limited`, field 2 is pitch, and
    an off-by-one between them would make each report the other's quantity."""
    (paths / "lat_interp").write_text("5,1.00,7.3,0.90,0.012")
    assert scc_shm.read_pitch_rate() == pytest.approx(7.3)
    assert scc_shm.read_eps_limited() is True
    (paths / "lat_interp").write_text("5,1.00,0.4,0.00,0.012")
    assert scc_shm.read_pitch_rate() == pytest.approx(0.4)
    assert scc_shm.read_eps_limited() is False
