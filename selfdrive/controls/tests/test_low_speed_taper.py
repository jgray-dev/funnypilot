import math

import pytest

from openpilot.selfdrive.controls.lib.low_speed_taper import low_speed_torque_scale


@pytest.mark.parametrize("v_ego, expected", [
  (0.0, 0.0),
  (2.2352, 0.5),  # 5 mph
  (4.4704, 1.0),  # 10 mph
  (10.0, 1.0),
  (40.0, 1.0),
  (-1.0, 0.0),
])
def test_scale_values(v_ego, expected):
  assert low_speed_torque_scale(v_ego) == pytest.approx(expected, abs=1e-6)


def test_monotonic_and_bounded():
  speeds = [i * 0.1 for i in range(100)]
  scales = [low_speed_torque_scale(v) for v in speeds]
  assert all(0.0 <= s <= 1.0 for s in scales)
  assert all(b >= a for a, b in zip(scales, scales[1:], strict=False))


@pytest.mark.parametrize("v_ego", [math.nan, math.inf, -math.inf])
def test_non_finite_speed_gives_zero(v_ego):
  assert low_speed_torque_scale(v_ego) == 0.0
