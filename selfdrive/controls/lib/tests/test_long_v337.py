"""FunnyPilot v3.3.7 — longitudinal changes, import-light invariants.

Covers: lead-approach urgency (long_shaping), SCC-M earlier approach envelope
(scc_map_v2 with injected readers), the SCC-M vision veto, and the SLA
pre-zone speed ramps. The MPC weight application itself needs acados and is
exercised on-device.
"""
import math

from openpilot.selfdrive.controls.lib.long_shaping import lead_urgency, URGENCY_MIN_DECEL, URGENCY_MAX_DECEL
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_map_v2 import SCCMapV2, _ARRIVAL_LEAD_T, _A_DECEL_APPROACH
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.vision_veto import (
  SccmVisionVeto, VETO_ARM_FRAMES, VETO_TRUST_T)
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.pre_zone import (
  pre_zone_decel_target, pre_zone_accel_target, PLAN_DECEL, PLAN_ACCEL_UP, DECEL_LEAD_T)

STOP_DISTANCE = 7.5  # mirror of long_mpc.STOP_DISTANCE (not importable without acados deps)


class TestLeadUrgency:
  def test_zero_in_ordinary_following(self):
    # same speed / opening / far lead: no urgency, weights bit-identical
    assert lead_urgency(40.0, 25.0, 25.0, STOP_DISTANCE) == 0.0
    assert lead_urgency(40.0, 20.0, 25.0, STOP_DISTANCE) == 0.0
    # gentle closing far away: required decel well under the comfort floor
    assert lead_urgency(120.0, 25.0, 22.0, STOP_DISTANCE) == 0.0

  def test_full_on_stopped_lead_close(self):
    # the takeover case: 25 m/s onto a stopped car 70 m out ->
    # req = 625 / (2 * 62.5) = 5.0 m/s^2 >> URGENCY_MAX_DECEL
    assert lead_urgency(70.0, 25.0, 0.0, STOP_DISTANCE) == 1.0

  def test_ramps_between_thresholds(self):
    # choose distance for req exactly midway between the thresholds
    req = (URGENCY_MIN_DECEL + URGENCY_MAX_DECEL) / 2.0
    v = 20.0
    d = v * v / (2.0 * req) + STOP_DISTANCE
    u = lead_urgency(d, v, 0.0, STOP_DISTANCE)
    assert abs(u - 0.5) < 1e-6

  def test_robust_to_bad_inputs(self):
    assert lead_urgency(float("nan"), 25.0, 0.0, STOP_DISTANCE) == 0.0
    assert lead_urgency(0.0, 25.0, 0.0, STOP_DISTANCE) == 1.0  # inside gap floor: max urgency


class TestSccMapEarliness:
  """SCC-M with injected position/velocity readers: the v3.3.7 envelope must
  reach the curve speed BEFORE the curve, with margin."""

  @staticmethod
  def make(points, pos=(0.0, 0.0)):
    return SCCMapV2(params=False, position_reader=lambda: pos,
                    velocities_reader=lambda: points)

  @staticmethod
  def pt(lat, lon, v):
    return {"latitude": lat, "longitude": lon, "velocity": v}

  def test_cap_reaches_curve_speed_before_the_turn(self):
    # a 12 m/s curve point ~111 m north (1e-3 deg lat); at the arrival-lead
    # distance the raw cap must already BE the curve speed (times trim)
    curve_v = 12.0
    scc = self.make([self.pt(0.0, 0.0, 30.0), self.pt(1e-3, 0.0, curve_v)])
    raw, v_curve, d = scc._raw_cap_from_map(v_cruise=30.0, trim=1.0)
    assert 100.0 < d < 122.0
    # at 111 m out the envelope allows sqrt(12^2 + 2*0.85*(111 - 12*3.5))
    d_eff = max(0.0, d - curve_v * _ARRIVAL_LEAD_T)
    assert abs(raw - math.sqrt(curve_v ** 2 + 2.0 * _A_DECEL_APPROACH * d_eff)) < 1e-6
    # inside the arrival-lead window the cap IS the curve speed: slowdown done
    scc2 = self.make([self.pt(0.0, 0.0, 30.0), self.pt(3e-4, 0.0, curve_v)])  # ~33 m
    raw2, _, d2 = scc2._raw_cap_from_map(v_cruise=30.0, trim=1.0)
    assert d2 < curve_v * _ARRIVAL_LEAD_T
    assert abs(raw2 - curve_v) < 1e-6

  def test_straight_road_unconstrained(self):
    scc = self.make([self.pt(0.0, 0.0, 30.0), self.pt(1e-3, 0.0, 29.9)])
    raw, _, _ = scc._raw_cap_from_map(v_cruise=25.0, trim=1.0)
    assert raw == CAP_INACTIVE  # mapd suggestion not below cruise


class TestVisionVeto:
  def run(self, veto, n, **kw):
    args = dict(sccm_constraining=True, curve_distance_m=60.0, v_ego=25.0,
                vision_ok=True, sccv_active=False, max_pred_lat_accel=0.3,
                a_lat_limit=2.4)
    args.update(kw)
    out = False
    for _ in range(n):
      out = veto.update(**args)
    return out

  def test_arms_on_sustained_straight_close_curve(self):
    veto = SccmVisionVeto()
    assert not self.run(veto, VETO_ARM_FRAMES - 1)
    assert self.run(veto, 1)  # one more frame arms it

  def test_never_beyond_vision_horizon(self):
    # curve 200 m out at 25 m/s = 8 s away: map may know what vision can't see
    veto = SccmVisionVeto()
    assert not self.run(veto, VETO_ARM_FRAMES * 3, curve_distance_m=VETO_TRUST_T * 25.0 + 1.0)

  def test_real_curvature_blocks_and_releases(self):
    veto = SccmVisionVeto()
    # model sees curvature: never arms
    assert not self.run(veto, VETO_ARM_FRAMES * 3, max_pred_lat_accel=1.2)
    # armed, then the model starts seeing the curve: releases immediately
    assert self.run(veto, VETO_ARM_FRAMES)
    assert not self.run(veto, 1, max_pred_lat_accel=1.6)

  def test_fails_safe(self):
    veto = SccmVisionVeto()
    assert not self.run(veto, VETO_ARM_FRAMES * 3, max_pred_lat_accel=float("inf"))  # broken plan
    assert not self.run(veto, VETO_ARM_FRAMES * 3, vision_ok=False)
    assert not self.run(veto, VETO_ARM_FRAMES * 3, sccv_active=True)  # vision agrees it's a corner
    assert not self.run(veto, VETO_ARM_FRAMES * 3, sccm_constraining=False)


class TestPreZoneRamps:
  def test_decel_far_away_is_unconstraining(self):
    # 1000 m to a 15 m/s zone: allowed-now speed far above any highway target
    assert pre_zone_decel_target(15.0, 1000.0) > 40.0

  def test_decel_reaches_target_at_lead_time(self):
    # inside the arrival-lead window the target IS the next zone's speed
    v_next = 15.0
    assert abs(pre_zone_decel_target(v_next, v_next * DECEL_LEAD_T * 0.9) - v_next) < 1e-9

  def test_decel_envelope_slope(self):
    v_next, d = 15.0, 200.0
    want = math.sqrt(v_next ** 2 + 2.0 * PLAN_DECEL * (d - v_next * DECEL_LEAD_T))
    assert abs(pre_zone_decel_target(v_next, d) - want) < 1e-9

  def test_accel_ramp_starts_at_the_right_distance_and_lands_on_target(self):
    v_cur, v_next = 20.0, 26.7  # ~45 -> 60 mph
    d_start = (v_next ** 2 - v_cur ** 2) / (2.0 * PLAN_ACCEL_UP)
    assert abs(pre_zone_accel_target(v_cur, v_next, d_start) - v_cur) < 1e-9   # ramp begins
    assert pre_zone_accel_target(v_cur, v_next, d_start + 50.0) == v_cur       # earlier: no raise
    assert abs(pre_zone_accel_target(v_cur, v_next, 0.0) - v_next) < 1e-9      # boundary: at target
    mid = pre_zone_accel_target(v_cur, v_next, d_start / 2.0)
    assert v_cur < mid < v_next                                                # monotone ramp

  def test_accel_noop_when_next_not_higher(self):
    assert pre_zone_accel_target(20.0, 15.0, 100.0) == 20.0
    assert pre_zone_accel_target(20.0, 20.0, 100.0) == 20.0

  def test_bad_inputs(self):
    assert pre_zone_decel_target(float("nan"), 100.0) == float("inf")
    assert pre_zone_decel_target(15.0, 0.0) == float("inf")
    assert pre_zone_accel_target(20.0, float("nan"), 100.0) == 20.0
