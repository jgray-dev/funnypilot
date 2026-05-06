"""
FunnyPilot LongV2 — physics unit tests.
Run: python -m pytest sunnypilot/selfdrive/controls/lib/long_v2/tests/test_physics.py
"""
import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../../.."))

import pytest

_G = 9.81
_K_SCCM = 0.78
_K_SCCV = 0.72


def corner_speed(fric, radius_m, k=_K_SCCM):
  return k * math.sqrt(fric * _G * radius_m)


def braking_start_dist(v_ego, v_target, decel=1.8, buffer=15.0):
  v_ego = max(v_ego, v_target)
  return (v_ego ** 2 - v_target ** 2) / (2 * decel) + buffer


# Corner speed for various R and FRIC combinations
@pytest.mark.parametrize("radius,fric,expected_min,expected_max", [
  (10,   0.3,  2.0, 4.0),
  (10,   0.8,  3.5, 5.5),
  (30,   0.5,  5.0, 8.0),
  (30,   0.8,  6.5, 9.5),
  (50,   0.5,  7.0, 10.0),
  (50,   0.8,  8.5, 12.5),
  (100,  0.5, 10.0, 14.5),
  (100,  0.8, 13.0, 18.0),
  (100,  1.0, 14.5, 20.0),
  (200,  0.5, 14.0, 20.0),
  (200,  0.8, 18.0, 26.0),
  (200,  1.0, 20.0, 28.0),
  (500,  0.3, 14.0, 20.0),
  (500,  0.8, 24.0, 32.0),
  (500,  1.0, 27.0, 35.0),
])
def test_corner_speed_range(radius, fric, expected_min, expected_max):
  v = corner_speed(fric, radius)
  assert expected_min <= v <= expected_max, f"R={radius}m FRIC={fric}: v={v:.2f} not in [{expected_min},{expected_max}]"


@pytest.mark.parametrize("v_ego,v_target,expected_min,expected_max", [
  (30.0, 10.0, 100.0, 200.0),
  (20.0, 15.0,  15.0,  60.0),
  (10.0, 10.0,  15.0,  16.0),  # no speed delta, just buffer
])
def test_braking_start_dist(v_ego, v_target, expected_min, expected_max):
  d = braking_start_dist(v_ego, v_target)
  assert expected_min <= d <= expected_max, f"v_ego={v_ego} v_target={v_target}: d={d:.1f} not in [{expected_min},{expected_max}]"


def test_jerk_filter_step():
  from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.jerk_filter import JerkFilter
  jf = JerkFilter(initial_a=0.0)
  dt = 0.05
  jerk_limit = 0.5
  # Step from 0 to 2.0 m/s², jerk-limited
  a = jf.update(2.0, dt, jerk_limit)
  assert abs(a - jerk_limit * dt) < 1e-6, f"Expected {jerk_limit*dt:.4f}, got {a:.4f}"
  # After many steps it should converge to 2.0
  for _ in range(200):
    a = jf.update(2.0, dt, jerk_limit)
  assert abs(a - 2.0) < 0.01


def test_jerk_filter_reset():
  from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.jerk_filter import JerkFilter
  jf = JerkFilter(initial_a=1.5)
  jf.reset(-1.0)
  assert jf.value == -1.0
  a = jf.update(0.0, 0.05, 3.0)
  assert abs(a - (-1.0 + 3.0 * 0.05)) < 1e-6


if __name__ == "__main__":
  pytest.main([__file__, "-v"])
