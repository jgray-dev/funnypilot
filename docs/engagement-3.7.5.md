# 3.7.5 engagement regression: msgq reader capacity

## Report and scope

liveCalibration and livePose arrive invalid, several dependent services with
them, and openpilot refuses to engage. The owner reports that this began with
`funnypilot-3.7.1a` and persists through `funnypilot-3.7.4`. This release
identifies the responsible 3.7.1a change from source, explains why 3.7.4 did
not resolve it, and removes the defect with a Python-only change plus a static
regression test. Release base: `funnypilot-3.7.4`,
`c2408e537f1376bcb6610336b82b07037529614a`.

No device data was available in this session: no recorded engagement event,
no swaglog, no live reader population. Everything below is a source finding.
The symptom set it predicts matches the report exactly, but attribution to the
owner's drives still needs a recording (see Verification and limits).

## Which 3.7.1a change

`git log funnypilot-3.7.1..funnypilot-3.7.1a` contains six commits. Only one
adds a msgq subscriber or a process:

| commit | change | new readers |
|---|---|---|
| 57a3117dc | steering PID transitions, planned braking | none |
| 43603f220 | signed wheel-motion preview | none |
| 18ac8b42d | merge of the 3.7.1 release | none |
| 0b3b6e28f | web UI drive saving, retention | none |
| **bc9c54436** | **Report recorder `funnypilot_feedback` (`always_run`)** | **12 subscriptions, carState polled at 100 Hz** |
| a949212af | alert visibility, calibration-gated overlays | none |

The controller, UI and retention commits change no subscriptions. The recorder
in `sunnypilot/feedback/feedbackd.py` subscribed to carState, controlsState,
longitudinalPlan, deviceState, selfdriveState, liveTorqueParameters,
carControl, radarState, longitudinalPlanSP, liveCalibration, onroadEvents and
onroadEventsSP, and because it is `always_run` it held those readers
permanently, offroad included.

## Mechanism

`msgq_repo/msgq/msgq.h` fixes `NUM_READERS` at 15 per service. In
`msgq_repo/msgq/msgq.cc`, `msgq_init_subscriber()` handles a 16th registration
by zeroing `num_readers`, clearing every `read_valids` and `read_uids` slot and
signalling all readers. `msgq_msg_recv()` and `msgq_msg_ready()` detect the
changed UID on the next receive, re-register, and `msgq_reset_reader()` moves
the new slot's read pointer to the current write pointer. Every evicted reader
therefore loses everything published between the eviction and its next
receive, and as soon as all sixteen are registered again the next registration
evicts everyone once more. With sixteen live readers this never settles.

The rate at which the cycle repeats is set by how quickly all sixteen readers
come back, which on carState is roughly the period of its 20 Hz readers. A
20 Hz reader is therefore evicted between almost every pair of its own
receives and, since every re-registration skips to the write pointer, it
receives nothing:

- `calibrationd` polls cameraOdometry at 20 Hz and reads carState non-blocking.
  It publishes `liveCalibration.valid = sm.all_checks()`, and carState is dead
  after 100 ms without a message (`10 / frequency` in `SubMaster`). It also
  never updates `v_ego`, so calibration cannot progress.
- `locationd` polls cameraOdometry at 20 Hz with carState and liveCalibration as
  inputs; livePose `inputsOK` becomes false.
- `paramsd`, `torqued` and `lagd` poll livePose and read carState; liveParameters,
  liveTorqueParameters and liveDelay follow.
- `selfdrived` then raises `commIssue`/`commIssueAvgFreq` (no entry) and logs the
  `invalid`, `not_alive` and `not_freq_ok` lists, which is the shape of the
  owner's report.

## Reader census

Static count of configured readers per service for a comma 3X driving a car
with default settings (stock model, logging on, Quectel GPS, no joystick or
maneuver mode), from each process's `SubMaster`/`sub_sock` literals, loggerd's
`should_log` services, and the native `locationd_llk` and `pandad` sources.
The optional column adds sunnylink registration and a paired Astra monitor.

| version | carState | deviceState (default / optional) |
|---|---|---|
| 3.7.1 | 15 | 12 / 15 |
| 3.7.1a to 3.7.3e | **16** | 13 / 16 to 17 |
| 3.7.4 | 15 (no spare slot) | 13 / 17 |
| **3.7.5** | **14** | **12 / 15** |

carState readers at 3.7.5: loggerd, selfdrived, controlsd, plannerd, radard,
lagd, torqued, calibrationd, locationd, paramsd, modeld, dmonitoringd, ui and
locationd_llk. deviceState readers include four athenad upload workers, two
in the UI process (`ui_state` and `SunnylinkState`) and, when registered, two
in sunnylinkd plus statsd_sp.

Why 3.7.4 was not enough: it removed the stock feedbackd's disabled LKAS
carState reader, which brought carState back to exactly fifteen. Any transient
sixteenth reader (athenad `getMessage`, a debug tool) then evicts the table,
and deviceState stayed at or over the limit whenever sunnylink or Astra ran.
3.7.4 was never flashed or validated, so its remaining margin was untested.

## Fix

- `selfdrive/car/card.py` publishes the carState scalars the recorder needs
  (`canValid`, `vEgo`, `vEgoRaw`, `aEgo`, `steeringAngleDeg`, `steeringTorque`,
  `steeringPressed`, `brakePressed`, `gasPressed`, `standstill`) to
  `/dev/shm/fp_carstate` right after sending carState, at 100 Hz, through
  `sunnypilot/feedback/carstate_shm.py`. This is the fork's established
  telemetry channel pattern (`long_v2/scc_shm.py`, `car/brake_light_shm.py`):
  an atomic single-line file with the writer's monotonic stamp, read
  best-effort, stale after 0.1 s. A prebuilt fork cannot raise `NUM_READERS`
  (native ABI) or add a capnp field (rebuild), and card is the carState source,
  so the mirror carries exactly what every subscriber sees.
- `sunnypilot/feedback/feedbackd.py` subscribes to neither carState nor
  deviceState. It polls controlsState for its 100 Hz cadence, samples from the
  mirror (null fields and `valid.carState = false` when card is not
  publishing), and gates uploads on manager's `IsOnroad` param and
  `HARDWARE.get_network_type()` every 2 s. `protocol.publish_motion` takes the
  mirror record, so Astra's stationary signal keeps its 2 s window.
- `sunnypilot/astra_link/state.py` takes `started` from the `IsOnroad` param;
  `started` implies ignition in hardwared, so the mode logic is unchanged and a
  missing param can only add "onroad".
- `sunnypilot/feedback/tests/test_reader_budget.py` performs the census from
  source on every run: no service may exceed fourteen readers in the default
  configuration or fifteen with every optional daemon, every configured
  process must be classified, every service list must resolve, the three
  modeld variants must subscribe identically, and neither feedback process may
  hold a carState or deviceState reader.

Nothing else changes: no schema, Params key, native code, calibration state,
process gates or controller behaviour. The recorder's telemetry keeps its field
names and gains `t_car` (the mirror stamp).

## Verification and limits

- `sunnypilot/feedback/tests` (99 tests, including the new channel and census
  tests) and the documented regression command (1445 passed, 1 skipped) pass on
  the host with Python 3.11. Ruff passes on `selfdrive/ sunnypilot/ system/ common/`.
- The census test fails against the 3.7.4 recorder (carState 15, over the
  fourteen-reader default budget) and against the 3.7.1a recorder (carState 16,
  over the table), and passes with this release.
- The 3.7.3e test `test_main_keeps_running_without_niceness_and_closes_socket`
  had been failing since `publish_motion` started reading `sm.recv_time`; it
  passes again with the mirror-based signature.
- `sunnypilot/astra_link/tests` has 15 failures on this host both before and
  after the change (environment: the device fixture cannot start here); no new
  failure is introduced and the Safety tests pass.
- Not exercised: native msgq on a device, the 100 Hz `/dev/shm` write in card
  (estimated well under 0.1 ms per frame on AGNOS tmpfs, best-effort and never
  raising), engagement, driving. The release is pushed for the owner to flash
  when safe; acceptance needs the recorded commit, `liveCalibration.valid`,
  `livePose.inputsOK` and the selfdrived `commIssue` log from an actual drive.
