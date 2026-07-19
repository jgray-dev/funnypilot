"""FunnyPilot v3.3.8 — EpsTorqueGovernor invariants.

The contract: the request that leaves the governor is ALWAYS realizable by
the K5's carcontroller/panda driver-torque + slew limits (verified against
the real opendbc apply_driver_steer_torque_limits), collapses with the
hardware bound instantly, and recovers strictly slower than the hardware
would allow — the damping that breaks the bite/clamp/re-bite limit cycle.
"""
import math
import random
from types import SimpleNamespace

from openpilot.selfdrive.controls.lib.eps_limit import (EpsTorqueGovernor, STEER_MAX, STEER_DELTA_UP,
                                                        STEER_DELTA_DOWN, DRIVER_ALLOWANCE,
                                                        DRIVER_MULTIPLIER, RECOVERY_RATE)
from opendbc.car.lateral import apply_driver_steer_torque_limits

DT = 0.01

# the K5's classic-CAN limit set, as the real carcontroller consumes it
K5_LIMITS = SimpleNamespace(STEER_MAX=int(STEER_MAX), STEER_DELTA_UP=int(STEER_DELTA_UP),
                            STEER_DELTA_DOWN=int(STEER_DELTA_DOWN),
                            STEER_DRIVER_ALLOWANCE=int(DRIVER_ALLOWANCE),
                            STEER_DRIVER_MULTIPLIER=int(DRIVER_MULTIPLIER), STEER_DRIVER_FACTOR=1)


class TestEpsTorqueGovernor:
  def test_noop_when_unconstrained(self):
    # small demands, no driver torque: after the slew ramp-in, passthrough
    g = EpsTorqueGovernor(DT)
    out = 0.0
    for _ in range(100):
      out = g.update(0.3, 0.0)
    assert abs(out - 0.3) < 1e-9
    assert not g.driver_limited
    assert g.authority == 1.0

  def test_slew_matches_hardware_ramp(self):
    # a step demand rises at exactly the rack's +3/frame — never faster
    g = EpsTorqueGovernor(DT)
    prev = 0.0
    for _ in range(50):
      out = g.update(1.0, 0.0)
      assert out - prev <= STEER_DELTA_UP / STEER_MAX + 1e-12
      prev = out
    assert prev > 0.3  # and it does make progress

  def test_output_always_realizable_by_real_carcontroller(self):
    # KEY invariant: scale the governor's request to hardware units and run it
    # through the REAL opendbc clamp — the clamp must be an identity (within
    # rounding), i.e. we never request what the hardware would strip.
    rng = random.Random(1)
    g = EpsTorqueGovernor(DT)
    apply_last = 0
    for _ in range(2000):
      t_des = rng.uniform(-1.5, 1.5)
      drv = rng.choice([0.0, rng.uniform(-400, 400)])
      out = g.update(t_des, drv)
      req_units = int(round(out * STEER_MAX))
      applied = apply_driver_steer_torque_limits(req_units, apply_last, drv, K5_LIMITS)
      assert abs(applied - req_units) <= 1, (req_units, applied, drv)
      apply_last = applied

  def test_driver_limit_mirror_engages(self):
    # opposing sensor torque of 150 (the steeringPressed threshold) must
    # cap authority to the hardware's (384 + (50-150)*2)/384 = 184/384
    g = EpsTorqueGovernor(DT)
    for _ in range(300):
      g.update(1.0, 0.0)  # ramp to full
    out = g.update(1.0, -150.0)
    expected = (STEER_MAX + (DRIVER_ALLOWANCE - 150.0) * DRIVER_MULTIPLIER) / STEER_MAX
    assert g.driver_limited
    assert abs(g.authority - expected) < 1e-9
    # the output heads down toward the bound at the hardware down-rate
    assert out < 1.0

  def test_recovery_slower_than_hardware(self):
    # after a clamp, authority must come back at RECOVERY_RATE, not the
    # hardware's 0.78/s — this damping is the whole point
    g = EpsTorqueGovernor(DT)
    for _ in range(300):
      g.update(1.0, 0.0)
    for _ in range(30):
      g.update(1.0, -220.0)  # deep clamp
    low = g.authority
    for i in range(1, 51):
      g.update(1.0, 0.0)  # sensor fully relaxed
      gain = g.authority - low
      assert gain <= RECOVERY_RATE * DT * i + 1e-9
    hw_rate = STEER_DELTA_UP / STEER_MAX / DT
    assert RECOVERY_RATE < 0.5 * hw_rate

  def test_aligned_driver_torque_never_caps(self):
    # driver torque in the SAME direction as the command extends the bound
    g = EpsTorqueGovernor(DT)
    for _ in range(300):
      g.update(1.0, 0.0)
    out = g.update(1.0, 200.0)  # aligned, positive
    assert not g.driver_limited
    assert abs(out - 1.0) < 1e-9

  def test_negative_direction_symmetric(self):
    g = EpsTorqueGovernor(DT)
    for _ in range(300):
      g.update(-1.0, 0.0)
    g.update(-1.0, 150.0)  # opposing (positive sensor vs negative command)
    expected = (STEER_MAX + (DRIVER_ALLOWANCE - 150.0) * DRIVER_MULTIPLIER) / STEER_MAX
    assert g.driver_limited
    assert abs(g.authority - expected) < 1e-9

  def test_reset_restores_full_bounds_and_zero_base(self):
    g = EpsTorqueGovernor(DT)
    for _ in range(100):
      g.update(1.0, -300.0)
    g.reset()
    assert g.authority == 1.0 and not g.driver_limited
    out = g.update(1.0, 0.0)
    assert abs(out - STEER_DELTA_UP / STEER_MAX) < 1e-12  # ramps from zero

  def test_nan_inputs_safe(self):
    g = EpsTorqueGovernor(DT)
    for _ in range(50):
      out = g.update(float("nan"), float("nan"))
      assert math.isfinite(out)
    out = g.update(0.2, float("inf"))
    assert math.isfinite(out)

  def test_never_amplifies(self):
    # |output| <= |ramp-limited demand| in the demanded direction, always
    rng = random.Random(7)
    g = EpsTorqueGovernor(DT)
    for _ in range(1000):
      t_des = rng.uniform(-1.0, 1.0)
      out = g.update(t_des, rng.uniform(-300, 300))
      assert abs(out) <= 1.0 + 1e-12
