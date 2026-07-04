
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.drive_helpers import (smooth_value, smooth_curvature, smooth_accel,
                                                            MAX_TARGET_LAT_JERK, MAX_TARGET_LONG_JERK_UP,
                                                            MAX_TARGET_LONG_JERK_DOWN, MIN_SPEED)


class TestSmoothCurvature:
  def test_small_changes_match_smooth_value(self):
    # for realistic frame-to-frame changes the jerk limiter must be inert
    v_ego = 25.0
    tau = 0.1
    prev = 0.001
    new = prev + 1e-5
    assert smooth_curvature(new, prev, v_ego, tau) == smooth_value(new, prev, tau)

  def test_spike_is_rate_limited(self):
    # a wildly different model output may only move the target by the jerk budget
    v_ego = 25.0
    prev = 0.0
    spike = 0.05
    max_step = MAX_TARGET_LAT_JERK / v_ego ** 2 * DT_MDL
    out = smooth_curvature(spike, prev, v_ego, 0.0)
    assert out == prev + max_step
    out = smooth_curvature(-spike, prev, v_ego, 0.0)
    assert out == prev - max_step

  def test_persistent_change_converges(self):
    # a real, persistent change still gets through, just ramped
    v_ego = 10.0
    target = 0.01
    val = 0.0
    for _ in range(200):
      val = smooth_curvature(target, val, v_ego, 0.1)
    assert abs(val - target) < 1e-4

  def test_one_frame_spike_mostly_rejected(self):
    # single bad output followed by sane outputs barely moves the action
    v_ego = 25.0
    max_step = MAX_TARGET_LAT_JERK / v_ego ** 2 * DT_MDL
    sane = 0.001
    val = sane
    val = smooth_curvature(0.05, val, v_ego, 0.1)  # outlier frame
    assert abs(val - sane) <= max_step * (1 + 1e-9)
    val = smooth_curvature(sane, val, v_ego, 0.1)  # recovers immediately
    assert abs(val - sane) <= max_step * (1 + 1e-9)

  def test_low_speed_uses_min_speed(self):
    out = smooth_curvature(1.0, 0.0, 0.0, 0.0)
    assert out == MAX_TARGET_LAT_JERK / MIN_SPEED ** 2 * DT_MDL


class TestSmoothAccel:
  def test_small_changes_match_smooth_value(self):
    prev = 0.5
    new = 0.52
    tau = 0.3
    assert smooth_accel(new, prev, tau) == smooth_value(new, prev, tau)

  def test_asymmetric_limits(self):
    # braking direction has a larger jerk budget than accel/brake-release
    prev = 0.0
    up = smooth_accel(10.0, prev, 0.0)
    down = smooth_accel(-10.0, prev, 0.0)
    assert up == MAX_TARGET_LONG_JERK_UP * DT_MDL
    assert down == -MAX_TARGET_LONG_JERK_DOWN * DT_MDL
    assert abs(down) > abs(up)

  def test_braking_not_meaningfully_delayed(self):
    # with the exponential filter disabled, a firm -3.0 m/s^2 step is ramped in
    # at the full braking jerk budget and reached within 0.4s
    val = 0.0
    for _ in range(int(3.0 / MAX_TARGET_LONG_JERK_DOWN / DT_MDL) + 1):
      val = smooth_accel(-3.0, val, 0.0)
    assert val == -3.0

    # with the stock tau, the limiter must add no more than one frame's jerk
    # budget of extra lag over the exponential filter alone
    limited, ema = 0.0, 0.0
    for _ in range(int(1.0 / DT_MDL)):
      limited = smooth_accel(-3.0, limited, 0.3)
      ema = smooth_value(-3.0, ema, 0.3)
      assert limited <= ema + MAX_TARGET_LONG_JERK_DOWN * DT_MDL

  def test_persistent_change_converges(self):
    val = 0.0
    for _ in range(100):
      val = smooth_accel(1.5, val, 0.3)
    assert abs(val - 1.5) < 1e-3
