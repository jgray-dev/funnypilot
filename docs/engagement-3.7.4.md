# 3.7.4 engagement investigation

## Scope and evidence limits

The owner reports that entering the driving UI works, but engagement is refused
with a calibration-data banner and a locationd temporary error, first noticed
between 3.7.1 and 3.7.3e. This release corrects a source-confirmed subscriber
capacity regression. The exact refusal has **not** been verified from a recorded
engagement event or a live reader population; vehicle validation remains pending.

Release base: `funnypilot-3.7.3e`,
`c318c073cfdfcb160f8ee41e6f4cd319cae2000c` (initially clean).
The linked device's fresh disk probe reported `funnypilot-3.7.3e`,
`c3454b7265653007c006e7d38444156f6ab7ebcc`, clean. Branch/version equality did
not imply commit equality. Startup reported the device's same commit, with dirty
state unknown. Sampled swaglog entries separately recorded that full device
commit and `dirty=false`; these entries were not the engagement event.

Identity age is unknown without a shared clock. The initial probe round trip was
11.885 seconds; its host receipt age at comparison was 24 ms. The diagnostic boot
identity journal has a wall-clock jump; do not align an older drive using boot
wall time alone. No recording-time identity for the affected attempt was obtained.

The linked device reported onroad/ignition on. Small allowed diagnostic reads
succeeded; a bounded command search was proposed for dedicated owner approval,
not executed during verification. Recording reads require offroad. The private
feedback inbox query failed because Wrangler lacked authenticated account access.
Sampled diagnostic messages show model-catalog DNS failures, which do not prove
the cause of the engagement refusal. No private recordings or raw logs are included
in this repository. No device command, flash, restart or settings change was made.

## Source-confirmed capacity defect

`msgq_repo/msgq/msgq.h` sets `NUM_READERS` to 15. In
`msgq_repo/msgq/msgq.cc`, `msgq_init_subscriber()` resets the reader allocation
and evicts all readers when another subscriber exceeds that limit. Existing
readers detect a changed UID in `msgq_msg_ready()`/`msgq_msg_recv()` and
re-register. With more than 15 simultaneously active readers, this can repeatedly
evict and resynchronize readers, losing messages. It is not permanent sharing of
one read pointer: the UID checks deliberately reconnect displaced readers.
Publisher initialization also resets the slots, so historical attachment counts
alone are not evidence of simultaneous overcapacity.

The normal C3X driving configuration has the following carState consumers:

1. loggerd (all logged services)
2. controlsd
3. selfdrived (dedicated socket)
4. plannerd
5. radard
6. locationd
7. calibrationd
8. paramsd
9. lagd
10. torqued
11. the selected modeld implementation (one, not all model variants)
12. ui
13. stock feedbackd
14. locationd_llk
15. dmonitoringd
16. funnypilot_feedback (added in 3.7.1a, `bc9c54436`)

This is a configured-process count, not a measured device-process count. Disabled
logging, nonstandard process exclusions or other model configurations may alter it.
The added Report recorder crosses the existing limit in the normal configuration.

## Why these symptoms are consistent with dropped inputs

- `calibrationd.main()` subscribes to cameraOdometry and carState, and publishes
  liveCalibration with validity taken from `sm.all_checks()`.
- `locationd.main()` includes liveCalibration and carState in its inputs. Its
  livePose `inputsOK` depends on input validity and critical observation checks.
- `selfdrived` adds `locationdTemporaryError` when `livePose.inputsOK` is false.
- The UI's `availability_message()` separately says "Calibration unavailable /
  Waiting for valid calibration data" when calibration data is not usable.

Thus one delivery failure can produce both symptoms without any calibration
parameter or localization-math change. Other causes (sensor sanity/timing errors,
model output validity, process failure) remain possible until the actual event is
read. The UI message alone does not prove this capacity defect caused that event.

## Fix

The stock `selfdrive/ui/feedback/feedbackd.py` has permanently disabled its LKAS
button handler (`if False`) since before this regression. Its carState and
selfdriveStateSP subscriptions served only that unreachable handler.

Replace the literal false condition with a named false constant and use the same
constant to add those two subscriptions only if LKAS handling is enabled. Normal
operation now uses no carState slot for this inactive feature, restoring the
15-reader budget. Raw-audio and bookmark subscriptions, bookmark publishing, the
new Report recorder and the existing disabled LKAS behavior are preserved.

Do not enable that handler or add another carState diagnostic subscriber without
addressing the capacity budget. This targeted Python-only fix leaves zero spare
slots in the normal configuration; it is not a general increase in IPC capacity.
No calibration reset, gate bypass, schema/Params change, native IPC ABI change,
controller retuning or unrelated Astra-link changes are part of this commit.

## Verification

- Python 3.11 source compilation: passed for stock feedbackd and the webserver
  version update.
- `git diff --check`: passed.
- Production feedbackd import: attempted, failed before module initialization
  because the host cannot load `msgq/ipc_pyx.so`. No mocked replacement was used.
- Ruff: unavailable in PATH and in the existing Python environment.
- Automated tests/suites: not run, per the owner's working instructions.
- Device execution, native IPC behavior, engagement and driving: not tested.

The release is pushed to GitHub for the owner to flash when safe. A future
acceptance check needs the exact refusal's recorded identity, calibration/input
validity and service-health flags; only then can this defect be attributed to the
reported drive. Do not mark a private feedback report fixed from this source
finding alone.
