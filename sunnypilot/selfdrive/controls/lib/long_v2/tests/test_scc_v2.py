"""
FunnyPilot v3.2.6e — SCC-Vision v2 and the shared curve-speed cap.

v3.6.2: the SCC-M half of this file moved to test_scc_map_v2.py when SCC-M
was rewritten around measured road geometry. What is left is SCC-V, which is
unchanged, and CurveSpeedCap, which both controllers still share — so a
change to it has to be seen to be safe for SCC-V too.

Import-light (numpy + long_v2 modules only):
  python3 -m pytest sunnypilot/selfdrive/controls/lib/long_v2/tests/test_scc_v2.py
"""
import math
from types import SimpleNamespace


from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import (
  CurveSpeedCap, CAP_INACTIVE, ACTIVATE_FRAMES, RELEASE_RATE,
)
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_vision_v2 import SCCVisionV2, lat_accel_limit
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.tuning import get_tuning

# v3.4.9: the comfort lat-accel target is a TUNING value, so the expectations
# below are derived from it rather than hardcoded (a retune used to silently
# invalidate three of these tests instead of failing them honestly).
A_LAT = get_tuning().a_lat_target

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
    v_corner = 30.0 * math.sqrt(A_LAT / 4.0)
    # cap must sit between corner speed and corner speed + decel budget
    assert v_corner - 0.5 < scc.output_v_target < 30.0
    assert scc.raw_v_target < 30.0 - 1.0

  def test_in_corner_holds_corner_speed(self):
    # corner is NOW (t=0..2): the raw cap is essentially the corner speed
    scc = SCCVisionV2(params=NoParams())
    model = make_model([(0.0, 2.0, 4.0)], v_plan=25.0)
    run_vision(scc, model, v_ego=25., v_cruise=30., n=200)
    v_corner = 25.0 * math.sqrt(A_LAT / 4.0)
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
    assert abs(lat_accel_limit(0.8) - A_LAT) < 1e-6
    assert lat_accel_limit(0.05) >= A_LAT * 0.7 - 1e-6
    assert lat_accel_limit(5.0) <= A_LAT * 1.1 + 1e-6


class TestSCCVisionSensitivity:
  """v3.4.9. The selection mask used to ask whether the MODEL'S OWN PLANNED
  trajectory exceeded the comfort limit. The model plans to slow for corners, so
  a corner it had already planned around read as "nothing to do" — and in `acc`
  mode the car never follows that planned velocity, so nothing slowed it."""

  def test_corner_the_model_planned_to_slow_for_is_still_caught(self):
    """MUTATION: mask on `orientationRate.z * velocity.x > a_lat_max` again.

    Same road curvature in both models; the second one is the model having
    planned to take it at a comfortable speed. The corner speed is a property of
    the ROAD and is identical — only the old mask disagreed.
    """
    # curvature chosen so the corner speed is ~19 m/s, well under a 30 m/s cruise
    curv = A_LAT / (19.0 ** 2)
    fast_plan = make_model([(0.5, 2.0, curv * 30.0 ** 2)], v_plan=30.0)   # would exceed the limit
    slow_plan = make_model([(0.5, 2.0, curv * 19.0 ** 2)], v_plan=19.0)   # model planned around it

    scc_fast = SCCVisionV2(params=NoParams())
    scc_slow = SCCVisionV2(params=NoParams())
    run_vision(scc_fast, fast_plan, v_ego=30., v_cruise=30., n=20)
    run_vision(scc_slow, slow_plan, v_ego=30., v_cruise=30., n=20)

    assert scc_slow.is_active, "a corner the model planned around is still a corner"
    assert abs(scc_slow.raw_v_target - scc_fast.raw_v_target) < 1.0

  def test_straight_road_still_produces_nothing(self):
    scc = SCCVisionV2(params=NoParams())
    run_vision(scc, make_model([]), v_ego=30., v_cruise=30., n=20)
    assert not scc.is_active
    assert scc.corroboration == 0.0

  def test_cap_does_not_evaporate_once_slowed_into_the_corner(self):
    """MUTATION: use v_ego alone as the reference speed instead of
    max(v_ego, v_cruise). Once the car has slowed TO the corner speed the mask
    would go empty, the cap would release, the car would speed back up, and the
    mask would fire again — an oscillation inside the corner."""
    scc = SCCVisionV2(params=NoParams())
    model = make_model([(0.0, 3.0, 4.0)], v_plan=30.0)
    run_vision(scc, model, v_ego=30., v_cruise=30., n=50)
    assert scc.is_active
    v_corner = 30.0 * math.sqrt(A_LAT / 4.0)
    run_vision(scc, model, v_ego=v_corner, v_cruise=30., n=50)
    assert scc.is_active
    assert scc.raw_v_target < CAP_INACTIVE


class TestCorroboration:
  """v3.4.9. The continuous signal that replaced the binary SCC-V veto."""

  def test_zero_on_a_straight_road(self):
    scc = SCCVisionV2(params=NoParams())
    run_vision(scc, make_model([]), v_ego=30., v_cruise=30., n=30)
    assert scc.corroboration == 0.0

  def test_saturates_well_below_sccv_activation(self):
    """The whole point of the merge: a corner the model clearly sees but that
    SCC-V has correctly decided it need not slow for must still corroborate."""
    scc = SCCVisionV2(params=NoParams())
    # lat accel at 30 m/s = 0.7 * A_LAT: over the corroboration knee, under the
    # comfort limit, so SCC-V itself has no business capping.
    model = make_model([(1.0, 3.0, 0.7 * A_LAT)], v_plan=30.0)
    run_vision(scc, model, v_ego=30., v_cruise=30., n=30)
    assert scc.corroboration > 0.99
    assert not scc.is_active

  def test_falls_slower_than_it_rises(self):
    scc = SCCVisionV2(params=NoParams())
    run_vision(scc, make_model([(0.0, 3.0, 4.0)]), v_ego=30., v_cruise=30., n=30)
    assert scc.corroboration > 0.99
    run_vision(scc, make_model([]), v_ego=30., v_cruise=30., n=3)
    assert scc.corroboration > 0.5, "must not blink out mid-corner"

  def test_cleared_when_vision_is_off(self):
    scc = SCCVisionV2(params=NoParams())
    run_vision(scc, make_model([(0.0, 3.0, 4.0)]), v_ego=30., v_cruise=30., n=30)
    assert scc.corroboration > 0.5
    run_vision(scc, make_model([(0.0, 3.0, 4.0)]), v_ego=2., v_cruise=30., n=1)
    assert scc.corroboration == 0.0, "an SCC-V below its speed floor corroborates nothing"
