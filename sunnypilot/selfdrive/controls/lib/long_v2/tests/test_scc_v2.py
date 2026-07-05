"""
FunnyPilot v3.2.6e — SCC-V / SCC-M v2 tests.

Import-light (numpy + long_v2 modules only):
  python3 -m pytest sunnypilot/selfdrive/controls/lib/long_v2/tests/test_scc_v2.py
"""
import math
from types import SimpleNamespace

from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import (
  CurveSpeedCap, CAP_INACTIVE, ACTIVATE_FRAMES, RELEASE_RATE,
)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_vision_v2 import SCCVisionV2, lat_accel_limit
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_map_v2 import SCCMapV2, speed_trim

DT = 0.05
N_PTS = 33
T_MAX = 10.0


class NoParams:
  """Params stub: features enabled."""

  def get_bool(self, key):
    return True


def make_model(lat_accels_by_time, v_plan=30.0):
  """lat_accels_by_time: list of (t_start, t_end, lat_accel) windows."""
  t_idxs = [T_MAX * (i / (N_PTS - 1)) ** 2 for i in range(N_PTS)]  # rough T_IDXS shape
  rates = []
  for t in t_idxs:
    lat_a = 0.0
    for t0, t1, a in lat_accels_by_time:
      if t0 <= t <= t1:
        lat_a = a
    rates.append(lat_a / max(v_plan, 1.0))
  return SimpleNamespace(
    orientationRate=SimpleNamespace(z=rates, t=t_idxs),
    velocity=SimpleNamespace(x=[v_plan] * N_PTS),
  )


def run_vision(scc, model, v_ego=30., v_cruise=30., fric=0.8, n=1):
  for _ in range(n):
    scc.update({"modelV2": model}, True, False, v_ego, 0.0, v_cruise, fric)


class TestCurveSpeedCap:
  def test_single_frame_noise_rejected(self):
    cap = CurveSpeedCap(DT)
    cap.update(20.0, 30.0, 30.0)
    cap.update(CAP_INACTIVE, 30.0, 30.0)
    assert not cap.active

  def test_activation_seeds_at_current_speed(self):
    cap = CurveSpeedCap(DT)
    for _ in range(ACTIVATE_FRAMES):
      v = cap.update(20.0, 28.0, 30.0)
    assert cap.active
    assert abs(v - 28.0) < 1e-6  # no step: cap starts at v_ego and eases down

  def test_tracks_down_and_releases_up(self):
    cap = CurveSpeedCap(DT)
    for _ in range(100):
      v = cap.update(20.0, 28.0, 30.0)
    assert abs(v - 20.0) < 0.2
    # raw constraint disappears: cap must rise rate-limited, then deactivate
    v_prev = v
    steps = 0
    while cap.active:
      v = cap.update(CAP_INACTIVE, 20.0, 30.0)
      if cap.active:
        assert v - v_prev <= RELEASE_RATE * DT + 1e-9
      v_prev = v
      steps += 1
      assert steps < 500
    # released over ~ (30-20)/2.5 = 4 s
    assert steps > int(8.0 / (RELEASE_RATE * DT))


class TestSCCVision:
  def test_straight_road_inactive(self):
    scc = SCCVisionV2(params=NoParams())
    run_vision(scc, make_model([]), n=10)
    assert not scc.is_active
    assert scc.output_v_target == CAP_INACTIVE

  def test_corner_ahead_caps_speed(self):
    # corner with 4.0 m/s^2 predicted lat accel from t=3s at plan speed 30
    scc = SCCVisionV2(params=NoParams())
    model = make_model([(3.0, 6.0, 4.0)], v_plan=30.0)
    run_vision(scc, model, v_ego=30., v_cruise=30., n=100)
    assert scc.is_active
    v_corner = 30.0 * math.sqrt(2.4 / 4.0)  # ~23.2
    # cap must sit between corner speed and corner speed + decel budget
    assert v_corner - 0.5 < scc.output_v_target < 30.0
    assert scc.raw_v_target < 30.0 - 1.0

  def test_in_corner_holds_corner_speed(self):
    # corner is NOW (t=0..2): the raw cap is essentially the corner speed
    scc = SCCVisionV2(params=NoParams())
    model = make_model([(0.0, 2.0, 4.0)], v_plan=25.0)
    run_vision(scc, model, v_ego=25., v_cruise=30., n=200)
    v_corner = 25.0 * math.sqrt(2.4 / 4.0)
    assert scc.is_active
    assert abs(scc.raw_v_target - v_corner) < 1.0
    assert abs(scc.output_v_target - v_corner) < 1.5

  def test_far_corner_constrains_less_than_near(self):
    scc_far = SCCVisionV2(params=NoParams())
    scc_near = SCCVisionV2(params=NoParams())
    run_vision(scc_far, make_model([(6.0, 7.0, 4.0)]), n=1)
    run_vision(scc_near, make_model([(0.0, 1.0, 4.0)]), n=1)
    assert scc_far.raw_v_target > scc_near.raw_v_target

  def test_release_after_corner(self):
    scc = SCCVisionV2(params=NoParams())
    run_vision(scc, make_model([(0.0, 2.0, 4.0)]), v_ego=23., n=100)
    assert scc.is_active
    run_vision(scc, make_model([]), v_ego=23., n=400)
    assert not scc.is_active
    assert scc.output_v_target == CAP_INACTIVE

  def test_low_speed_disabled(self):
    scc = SCCVisionV2(params=NoParams())
    run_vision(scc, make_model([(0.0, 2.0, 4.0)]), v_ego=3., n=10)
    assert not scc.is_active

  def test_fric_influence_bounded(self):
    # the friction estimate is not road grip: it may only trim +/-30%
    assert abs(lat_accel_limit(0.8) - 2.4) < 1e-6
    assert lat_accel_limit(0.05) >= 2.4 * 0.7 - 1e-6
    assert lat_accel_limit(5.0) <= 2.4 * 1.1 + 1e-6


def route_north(v_by_index, spacing_deg=0.0005):
  """Route points going north; ~55.6 m spacing per 0.0005 deg."""
  return [{"latitude": i * spacing_deg, "longitude": 0.0, "velocity": v}
          for i, v in enumerate(v_by_index)]


def make_map_scc(points, pos=(0.0, 0.0)):
  return SCCMapV2(params=NoParams(),
                  position_reader=lambda: pos,
                  velocities_reader=lambda: points)


def run_map(scc, v_ego=30., v_cruise=30., fric=0.8, n=1):
  for _ in range(n):
    scc.update({}, True, False, v_ego, 0.0, v_cruise, fric)


class TestSCCMap:
  def test_no_data_inactive(self):
    scc = make_map_scc([])
    run_map(scc, n=10)
    assert not scc.is_active
    assert scc.output_v_target == CAP_INACTIVE

  def test_slow_curve_ahead_braking_envelope(self):
    # 10 m/s curve point ~222 m ahead; envelope: sqrt(v_c^2 + 2*a*d_eff)
    points = route_north([30, 30, 30, 30, 10, 30])
    scc = make_map_scc(points)
    run_map(scc, n=200)
    assert scc.is_active
    v_curve = 10 * speed_trim(0.8)
    d = 4 * 55.66
    d_eff = d - v_curve * 2.0
    expected = math.sqrt(v_curve ** 2 + 2.0 * 1.0 * d_eff)
    assert abs(scc.raw_v_target - expected) < 1.0
    assert abs(scc.output_v_target - expected) < 1.5

  def test_cap_tightens_as_curve_approaches(self):
    points = route_north([30, 30, 30, 30, 10, 30])
    far = make_map_scc(points, pos=(0.0, 0.0))
    near = make_map_scc(points, pos=(0.0015, 0.0))  # ~55 m from the slow point
    run_map(far, n=1)
    run_map(near, n=1)
    assert near.raw_v_target < far.raw_v_target

  def test_passed_curve_releases(self):
    points = route_north([30, 30, 30, 30, 10, 30, 30])
    scc = make_map_scc(points, pos=(0.0025, 0.0))  # past the slow point
    run_map(scc, n=10)
    assert not scc.is_active

  def test_curve_faster_than_cruise_ignored(self):
    points = route_north([30, 30, 28, 30])
    scc = make_map_scc(points)
    run_map(scc, v_cruise=20., n=10)
    assert not scc.is_active

  def test_radius_estimate_published(self):
    points = route_north([30, 30, 10, 30])
    scc = make_map_scc(points, pos=(0.0005, 0.0))
    run_map(scc, n=50)
    assert scc.is_active
    assert scc.corner_radius_m > 0
