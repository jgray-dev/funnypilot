"""FunnyPilot v3.2.6e — invariants of the longitudinal output shaping.

Import-light (numpy only), so it runs without the full openpilot environment:
  python3 -m pytest selfdrive/controls/lib/tests/test_long_shaping.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../.."))

from openpilot.selfdrive.controls.lib.long_shaping import (
  AccelJerkShaper, LeadGrace,
  JERK_DOWN_BP, JERK_DOWN_V,
  LEAD_GRACE_ARM_TIME, LEAD_GRACE_HOLD_TIME, LEAD_GRACE_RELEASE_TIME,
)

DT = 0.05
EPS = 1e-9


class TestAccelJerkShaper:
  def test_up_jerk_limited(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    out = s.update(2.0, jerk_up=1.8)
    assert abs(out - 1.8 * DT) < EPS

  def test_up_converges_without_overshoot(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    prev = 0.0
    for _ in range(100):
      out = s.update(1.0, jerk_up=1.8)
      assert prev - EPS <= out <= 1.0 + EPS
      prev = out
    assert abs(out - 1.0) < EPS

  def test_mild_braking_comfort_limited(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    out = s.update(-1.0)
    # mild demand: bounded by the comfort down-jerk (4 m/s^3)
    assert abs(out - (-JERK_DOWN_V[1] * DT)) < EPS

  def test_strong_braking_barely_delayed(self):
    # SAFETY INVARIANT: a -3.5 m/s^2 demand must be reached in well under
    # half a second. At the max down-jerk of 12 m/s^3 that is <= 6 frames.
    s = AccelJerkShaper(DT, a_init=0.0)
    frames = 0
    while s.update(-3.5) > -3.5 + EPS:
      frames += 1
      assert frames < 10, "strong braking demand delayed too long"
    assert frames <= int(3.5 / (JERK_DOWN_V[0] * DT)) + 1

  def test_down_jerk_scales_with_demand(self):
    mild = AccelJerkShaper(DT, a_init=0.0).update(JERK_DOWN_BP[1])
    strong = AccelJerkShaper(DT, a_init=0.0).update(JERK_DOWN_BP[0])
    assert strong < mild < 0.0

  def test_fcw_bypass_is_immediate(self):
    s = AccelJerkShaper(DT, a_init=1.0)
    out = s.update(-4.0, bypass=True)
    assert out == -4.0
    # and the shaper re-seeds from the bypassed value
    assert s.update(-4.0) == -4.0

  def test_brake_release_is_gentle(self):
    s = AccelJerkShaper(DT, a_init=-2.0)
    out = s.update(0.0, jerk_up=1.8)
    assert abs(out - (-2.0 + 1.8 * DT)) < EPS

  def test_reset(self):
    s = AccelJerkShaper(DT, a_init=0.0)
    s.update(1.0)
    s.reset(-0.5)
    assert s.a == -0.5

  def test_nan_target_contained(self):
    s = AccelJerkShaper(DT, a_init=0.5)
    out = s.update(float("nan"))
    assert out == 0.0


def run_grace(g, seconds, **kw):
  out = None
  for _ in range(int(round(seconds / DT))):
    out = g.update(**kw)
  return out


class TestLeadGrace:
  def test_no_lead_never_tracked_passthrough(self):
    g = LeadGrace(DT)
    assert g.update(False, False, 0.0, 20.0, 30.0) == 30.0

  def test_flicker_caps_at_lead_speed(self):
    g = LeadGrace(DT)
    # follow a 15 m/s lead for 2 s, then it flickers off
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=15.0, v_ego=15.0, v_cruise=30.0)
    out = g.update(False, False, 0.0, 15.0, 30.0)
    assert abs(out - 15.0) < EPS

  def test_cap_never_below_v_ego(self):
    # ROBUSTNESS INVARIANT: grace may hold the car back, never brake it.
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=10.0, v_ego=10.0, v_cruise=30.0)
    # ego is now faster than the lead was (e.g. lead data was stale)
    out = g.update(False, False, 0.0, 18.0, 30.0)
    assert out >= 18.0 - EPS

  def test_short_lived_lead_not_armed(self):
    g = LeadGrace(DT)
    # lead only seen for 0.5 s (< ARM time): its loss has no authority
    run_grace(g, min(0.5, LEAD_GRACE_ARM_TIME / 2), lead_status=True, following=True, v_lead=5.0, v_ego=20.0, v_cruise=30.0)
    assert g.update(False, False, 0.0, 20.0, 30.0) == 30.0

  def test_not_following_not_armed(self):
    g = LeadGrace(DT)
    # lead visible but not the MPC's constraint (far ahead)
    run_grace(g, 3.0, lead_status=True, following=False, v_lead=5.0, v_ego=20.0, v_cruise=30.0)
    assert g.update(False, False, 0.0, 20.0, 30.0) == 30.0

  def test_release_ramp_and_full_release(self):
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=15.0, v_ego=15.0, v_cruise=30.0)
    # hold phase
    out_hold = run_grace(g, LEAD_GRACE_HOLD_TIME, lead_status=False, following=False, v_lead=0.0, v_ego=15.0, v_cruise=30.0)
    assert abs(out_hold - 15.0) < 0.5
    # mid-ramp: strictly between hold speed and cruise
    out_mid = run_grace(g, LEAD_GRACE_RELEASE_TIME / 2, lead_status=False, following=False, v_lead=0.0, v_ego=15.0, v_cruise=30.0)
    assert 15.0 < out_mid < 30.0
    # fully released
    out_end = run_grace(g, LEAD_GRACE_RELEASE_TIME, lead_status=False, following=False, v_lead=0.0, v_ego=15.0, v_cruise=30.0)
    assert out_end == 30.0

  def test_cap_respects_lower_cruise(self):
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=25.0, v_ego=20.0, v_cruise=30.0)
    # user set speed below the cap: cruise wins
    assert g.update(False, False, 0.0, 20.0, 22.0) == 22.0

  def test_reacquire_resets(self):
    g = LeadGrace(DT)
    run_grace(g, 2.0, lead_status=True, following=True, v_lead=15.0, v_ego=15.0, v_cruise=30.0)
    g.update(False, False, 0.0, 15.0, 30.0)
    # new lead appears: loss timer resets, tracking restarts
    g.update(True, True, 12.0, 15.0, 30.0)
    assert g._lost_t == 0.0
