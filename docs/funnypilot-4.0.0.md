# FunnyPilot 4.0.0

Fresh fork of sunnypilot tag `v2026.002.003` (openpilot 0.11.2). None of the 3.7.x
customisations are carried over; this cut adds only the four changes below.
Target: Kia K5 2021, comma 3X, Hyundai CAN, MADS lateral + stock cruise.

## AGNOS

Requires AGNOS 18.4 (`launch_env.sh` pins `AGNOS_VERSION="18.4"`). A device still on
AGNOS 16 will automatically flash the OS on first boot of this branch.

## Changes

1. **No seatbelt disengage/no-entry** - `selfdrive/car/car_specific.py` no longer adds
   `seatbeltNotLatched`. MADS (`sunnypilot/mads/`) only remaps that event, so nothing
   else raises it.
2. **No ESC/TCS-off disengage/no-entry** - same file, `espDisabled` is no longer added.
   `espActive` (ESC intervening) is unchanged.
3. **Driver monitoring disabled, compute freed**
   - `system/manager/process_config.py`: `dmonitoringmodeld` runs only when
     `IsDriverViewEnabled` is set (driver view / mounting check), not onroad.
     `camerad` and `driverCameraState` are untouched.
   - `selfdrive/monitoring/dmonitoringd.py`: publishes a constant valid
     `driverMonitoringState` at 20 Hz (alertLevel none, no lockouts, wheeltouch policy,
     `isRHD` from `IsRhdDetected`). `selfdrive/monitoring/policy.py` is unused at runtime.
   - `selfdrive/selfdrived/selfdrived.py`: DM event/lockout block gated by
     `DM_DISABLED = True`; the `longitudinalPlanSP` event merge is preserved.
4. **Low-speed lateral torque taper** - `selfdrive/controls/lib/low_speed_taper.py`
   (`low_speed_torque_scale`), applied in `controlsd.py` after `LaC.update`: 0% at
   0 mph, 50% at 5 mph, 100% at >= 10 mph. Torque-control cars only (angle cars skipped).
   Test: `selfdrive/controls/tests/test_low_speed_taper.py`.

## Caveats

- With ESC/TCS off the K5 may report `accFaulted` from the car itself (unverified); that
  path is unchanged and would still disengage/block engagement.
- Process replay for `dmonitoringd` and `test_onroad` DM expectations no longer match.
