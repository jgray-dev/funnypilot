"""FunnyPilot v3.4.5 — clock-domain guard for the map-data freshness test.

THE BUG THIS PINS. Upstream computed map-data age as

    time.monotonic() - sm['gpsLocation'].unixTimestampMillis * 1e-3

which subtracts a UNIX EPOCH (~1.8e9) from a SINCE-BOOT counter (~1e4). The
result on this device was about -1.785e9 seconds. Two consequences, both
silent:

  * the staleness gate `age > LIMIT_MAX_MAP_DATA_AGE` could never fire for a
    real fix. It only rejected `unixTimestampMillis == 0`, i.e. "no GPS fix has
    ever arrived" — an accidental has-ever-had-a-fix test wearing a freshness
    test's clothes.
  * `distance_since_fix = v_ego * age` came out near -3.9e10 m at highway
    speed, so `distance_to_next_limit` was ~3.9e10 m. EVERY consumer of that
    number — SLA's pre-zone gas gate and the v3.4.0 predictive set-speed ramp —
    was therefore dead code on a moving car while looking perfectly alive.

Nothing caught it because the SLA tests fed `next_distance` in directly, so
they exercised the consumers with hand-written metres and never went through
the producer. These tests go through the producer.

Import-light: cereal.messaging and common.params need compiled extensions on
this machine, so they are stubbed before the module under test is imported.
Only the arithmetic is under test, and it is pure Python.
"""
import sys
import time
import types

import pytest


def _stub_compiled_deps():
  if 'cereal.messaging' not in sys.modules:
    m = types.ModuleType('cereal.messaging')
    m.SubMaster = type('SubMaster', (), {})
    sys.modules['cereal.messaging'] = m

  if 'openpilot.common.params' not in sys.modules:
    m = types.ModuleType('openpilot.common.params')

    class _Params:
      def __init__(self, *a, **k):
        pass

      def get(self, key, *a, **k):
        return 0

      def get_bool(self, *a, **k):
        return False

      def put(self, *a, **k):
        pass

    m.Params = _Params
    m.UnknownKeyName = type('UnknownKeyName', (Exception,), {})
    sys.modules['openpilot.common.params'] = m

  if 'openpilot.common.gps' not in sys.modules:
    m = types.ModuleType('openpilot.common.gps')
    m.get_gps_location_service = lambda *a, **k: 'gpsLocation'
    sys.modules['openpilot.common.gps'] = m


_stub_compiled_deps()

try:
  from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit import speed_limit_resolver as res_mod
except ImportError as e:  # pragma: no cover - environment guard
  pytest.skip(f"unrelated compiled dependency unavailable: {e}", allow_module_level=True)

from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Policy, OffsetType


class FakeMapData:
  def __init__(self, limit=0., ahead=0., ahead_dist=0.):
    self.speedLimit = limit
    self.speedLimitValid = limit > 0.
    self.speedLimitAhead = ahead
    self.speedLimitAheadValid = ahead > 0.
    self.speedLimitAheadDistance = ahead_dist


class FakeCarStateSP:
  speedLimit = 0.


class FakeSM:
  """Only the three SubMaster surfaces the resolver touches."""

  def __init__(self, map_data, age=0.0, valid=True):
    self._map_data = map_data
    # recv_time is stamped by SubMaster with the CONSUMER's time.monotonic(),
    # regardless of what language published the message. That is the whole
    # point: it cannot be in a different clock domain than we are.
    self.recv_time = {'liveMapDataSP': time.monotonic() - age}
    self.valid = {'liveMapDataSP': valid}

  def __getitem__(self, key):
    if key == 'liveMapDataSP':
      return self._map_data
    if key == 'carStateSP':
      return FakeCarStateSP()
    raise KeyError(key)


def make_resolver():
  r = res_mod.SpeedLimitResolver()
  r.policy = Policy.map_data_only
  r.offset_type = OffsetType.off
  r.offset_value = 0
  r.is_metric = False
  return r


class TestFreshDataProducesMetres:
  def test_distance_is_metres_not_astronomical_units(self):
    """MUTATION: go back to subtracting a unix stamp from time.monotonic().

    The single assertion that would have caught the whole bug: at 30 m/s with
    the boundary 300 m out, the answer must be ~300 m, not ~4e10.
    """
    r = make_resolver()
    sm = FakeSM(FakeMapData(limit=25., ahead=13.4, ahead_dist=300.), age=0.1)
    r.update(30., sm)
    assert 293. < r.distance_to_next_limit <= 300.
    assert abs(r.next_speed_limit - 13.4) < 1e-6

  def test_age_compensation_moves_the_boundary_closer(self):
    """The correction is real, not a no-op: 1 s of travel at 30 m/s is 30 m."""
    r = make_resolver()
    sm = FakeSM(FakeMapData(limit=25., ahead=13.4, ahead_dist=300.), age=1.0)
    r.update(30., sm)
    assert abs(r.distance_to_next_limit - 270.) < 1.0

  def test_standstill_needs_no_correction(self):
    r = make_resolver()
    sm = FakeSM(FakeMapData(limit=25., ahead=13.4, ahead_dist=300.), age=1.5)
    r.update(0., sm)
    assert abs(r.distance_to_next_limit - 300.) < 1e-6


class TestStaleness:
  def test_no_message_yet_is_rejected(self):
    """recv_time is 0. until the first message arrives. The old code's gate
    passed in this state (a zero unix stamp read as 'infinitely old' only by
    accident); here it must reject."""
    r = make_resolver()
    sm = FakeSM(FakeMapData(limit=25., ahead=13.4, ahead_dist=300.))
    sm.recv_time['liveMapDataSP'] = 0.
    r.update(30., sm)
    assert r.distance_to_next_limit == 0.
    assert r.next_speed_limit == 0.

  def test_invalid_is_rejected(self):
    """sm.valid for this service is literally llk.gpsOK (base_map_data.py) —
    the GPS-fix condition the epoch subtraction was groping for."""
    r = make_resolver()
    sm = FakeSM(FakeMapData(limit=25., ahead=13.4, ahead_dist=300.), valid=False)
    r.update(30., sm)
    assert r.distance_to_next_limit == 0.

  def test_stale_message_is_rejected(self):
    """MUTATION: delete the age gate."""
    r = make_resolver()
    sm = FakeSM(FakeMapData(limit=25., ahead=13.4, ahead_dist=300.),
                age=res_mod.MAP_MSG_MAX_AGE + 0.5)
    r.update(30., sm)
    assert r.distance_to_next_limit == 0.

  def test_one_dropped_message_is_tolerated(self):
    """liveMapDataSP is 1 Hz, so the gate must survive a single drop or the
    upcoming-zone info would blink out constantly."""
    assert res_mod.MAP_MSG_MAX_AGE > 1.0
    r = make_resolver()
    sm = FakeSM(FakeMapData(limit=25., ahead=13.4, ahead_dist=300.), age=1.2)
    r.update(30., sm)
    assert r.distance_to_next_limit > 0.


class TestNoEpochArithmetic:
  """Source guards. The bug was invisible at runtime for two years of forks;
  the cheapest permanent defence is to make the wrong symbols unusable."""

  def _src(self):
    import pathlib
    return pathlib.Path(res_mod.__file__).read_text()

  def test_gps_unix_timestamp_is_never_read(self):
    code = '\n'.join(ln.split('#', 1)[0] for ln in self._src().splitlines())
    assert 'unixTimestampMillis' not in code, \
      "a unix epoch stamp is not comparable with time.monotonic() — see this module's docstring"

  def test_old_age_constant_stays_removed(self):
    code = '\n'.join(ln.split('#', 1)[0] for ln in self._src().splitlines())
    assert 'LIMIT_MAX_MAP_DATA_AGE' not in code

  def test_recv_time_is_what_ages_the_message(self):
    """Anti-vacuity: the two assertions above would also pass on an empty file."""
    code = self._src()
    assert "sm.recv_time['liveMapDataSP']" in code
    assert 'time.monotonic()' in code

  def test_log_mono_time_is_not_used_either(self):
    """logMonoTime is time.monotonic() for Python publishers but CLOCK_BOOTTIME
    for C++ ones (cereal/messaging/__init__.py vs common/timing.h), so swapping
    it in would reintroduce exactly this bug class through a different door."""
    code = '\n'.join(ln.split('#', 1)[0] for ln in self._src().splitlines())
    assert 'logMonoTime' not in code
