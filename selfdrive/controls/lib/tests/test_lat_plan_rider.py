"""FunnyPilot v3.2.9e — PlanRider invariants.

Import-light (numpy + drive_helpers + modeld constants):
  python3 -m pytest selfdrive/controls/lib/tests/test_lat_plan_rider.py

The key claims of the deep reset, proven here:
  * riding a plan produces per-frame-smooth output (no 20 Hz staircase)
  * plan handoffs are continuous when successive plans agree
  * disagreements (model revisions) are bounded by the lateral-jerk clamp
  * cadence robustness: a late plan is ridden further, not stalled on
  * degraded modes: stale plan -> hold; no plan -> model action fallback
"""
import numpy as np

from openpilot.selfdrive.controls.lib.lat_plan_rider import (
  PlanRider, DT_MDL, RIDE_EXTRA_S, MAX_TARGET_LAT_JERK, HEALTH_FULL, T_IDXS, N_PLAN,
)

DT = 0.01  # 100 Hz
V = 20.0   # m/s
DELAY = 0.5


def const_curv_plan(k, v=V):
  """Plan for driving a constant-curvature arc: psi(t) = k*v*t."""
  yaws = k * v * T_IDXS
  yaw_rates = np.full(N_PLAN, k * v)
  return yaws, yaw_rates


def ramp_plan(a, t_capture, v=V):
  """Stationary curvature ramp k(t_wall) = a*t_wall, captured at t_capture:
  psi(tau) = v*a*(t_capture*tau + tau^2/2), yaw_rate(tau) = v*a*(t_capture+tau)."""
  yaws = v * a * (t_capture * T_IDXS + T_IDXS ** 2 / 2)
  yaw_rates = v * a * (t_capture + T_IDXS)
  return yaws, yaw_rates


class TestPlanRider:
  def test_constant_curvature_plan_tracks_exactly(self):
    r = PlanRider(DT)
    r.reset(0.0)
    k = 3e-4
    yaws, rates = const_curv_plan(k)
    assert r.set_plan(yaws, rates, 0.0)
    out = 0.0
    for i in range(100):
      out = r.update(i * DT, DELAY, V, fallback=0.0)
    assert abs(out - k) < 1e-9

  def test_no_staircase_on_ramp(self):
    # curvature ramps at a rad/m/s; plans arrive at 20 Hz; the output must
    # advance EVERY 100 Hz frame by ~a*dt — never sit flat then jump 5x.
    r = PlanRider(DT)
    a = 2e-5
    r.set_plan(*ramp_plan(a, 0.0), 0.0)
    r.reset(a * (DELAY + DT_MDL))  # start on-trajectory
    r.set_plan(*ramp_plan(a, 0.0), 0.0)
    outs = []
    for i in range(1, 101):
      t = i * DT
      if i % 5 == 0:  # fresh plan every 50 ms
        r.set_plan(*ramp_plan(a, t), t)
      outs.append(r.update(t, DELAY, V, fallback=0.0))
    diffs = np.diff(outs)
    assert np.all(diffs > 0.5 * a * DT)   # advances every frame
    assert np.all(diffs < 2.0 * a * DT)   # never a 5x staircase step

  def test_handoff_continuous_with_consistent_plans(self):
    r = PlanRider(DT)
    a = 2e-5
    r.reset(a * (DELAY + DT_MDL))
    r.set_plan(*ramp_plan(a, 0.0), 0.0)
    # ride to the end of the model period, then hand off to the next plan
    out_before = None
    for i in range(1, 6):
      out_before = r.update(i * DT, DELAY, V, fallback=0.0)
    r.set_plan(*ramp_plan(a, 0.05), 0.05)
    out_after = r.update(0.06, DELAY, V, fallback=0.0)
    # handoff step stays a normal-sized per-frame step (np.interp over the
    # non-uniform T_IDXS adds a tiny, physically negligible wiggle)
    step = out_after - out_before
    assert 0.0 < step < 3.0 * a * DT

  def test_model_revision_bounded_by_jerk_clamp(self):
    r = PlanRider(DT)
    r.reset(0.0)
    r.set_plan(*const_curv_plan(0.0), 0.0)
    r.update(DT, DELAY, V, fallback=0.0)
    # the model changes its mind hard: new plan wants a big curvature NOW
    r.set_plan(*const_curv_plan(5e-3), 0.02)
    out_prev = r.out
    out = r.update(0.03, DELAY, V, fallback=0.0)
    max_step = MAX_TARGET_LAT_JERK / V ** 2 * DT
    assert abs(out - out_prev) <= max_step + 1e-12

  def test_late_model_frame_keeps_riding(self):
    # cadence robustness: no new plan for 150 ms — output must KEEP advancing
    # along the stored plan (the model's own intent), not stall.
    r = PlanRider(DT)
    a = 2e-5
    r.reset(a * (DELAY + DT_MDL))
    r.set_plan(*ramp_plan(a, 0.0), 0.0)
    outs = [r.update(i * DT, DELAY, V, fallback=0.0) for i in range(1, 16)]
    diffs = np.diff(outs)
    assert np.all(diffs > 0.5 * a * DT)
    assert r.health_frames > 0.0

  def test_stale_plan_holds_and_health_decays(self):
    r = PlanRider(DT)
    r.reset(0.0)
    r.set_plan(*const_curv_plan(1e-4), 0.0)
    for i in range(1, 5):  # stay within one model period (age <= 0.04 s)
      r.update(i * DT, DELAY, V, fallback=0.0)
    assert r.health_frames == HEALTH_FULL
    # far past ride headroom: output holds, health hits 0
    out_a = r.update(DT_MDL + RIDE_EXTRA_S + 0.1, DELAY, V, fallback=0.0)
    out_b = r.update(DT_MDL + RIDE_EXTRA_S + 0.2, DELAY, V, fallback=0.0)
    assert out_a == out_b
    assert r.health_frames == 0.0

  def test_no_plan_uses_fallback(self):
    r = PlanRider(DT)
    r.reset(0.0)
    out = None
    for i in range(200):
      out = r.update(i * DT, DELAY, V, fallback=2e-4)
    assert abs(out - 2e-4) < 1e-9  # converged to the model action, jerk-limited

  def test_bad_plans_rejected(self):
    r = PlanRider(DT)
    r.reset(0.0)
    assert not r.set_plan([1.0, 2.0], [0.1], 0.0)                      # too short
    yaws, rates = const_curv_plan(1e-4)
    yaws = yaws.copy()
    yaws[3] = float("nan")
    assert not r.set_plan(yaws, rates, 0.0)                            # non-finite
    assert r.set_plan(*const_curv_plan(1e-4), 0.0)                     # good one accepted

  def test_reset_seeds_at_current_curvature(self):
    r = PlanRider(DT)
    r.set_plan(*const_curv_plan(0.0), 0.0)
    r.update(DT, DELAY, V, fallback=0.0)
    r.reset(4e-4)
    assert r.out == 4e-4
    assert r.health_frames == 0.0  # plan cleared; re-engage starts clean
