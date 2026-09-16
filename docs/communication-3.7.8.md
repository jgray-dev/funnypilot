# 3.7.8: hardware status stalls behind Params storage

Based on clean 3.7.7 `daef46d950d30a9d9993951d40b78c6d09833e9c`.
The owner reported more frequent red communication takeovers and explicitly
requested branch 3.7.8 plus automatic installation. SSH access was restored.
Private recordings/logs are retained outside Git in `/tmp/comma-378`.

## Recorded evidence

The device identifies as clean 3.7.7 at the above commit. Its process-health
history and matching swaglogs record four driving episodes on September 16 UTC:

| First failed check | Speed (m/s) | Last gap release (monotonic s) | Fault duration |
| --- | ---: | ---: | ---: |
| 00:23:01 | 10.35 | 811.97 (3.38 s before fault) | 4.40 s |
| 00:25:18 | 12.58 | 948.11 (4.64 s before fault) | 2.91 s |
| 00:41:00 | 36.15 | 948.11 (946 s before fault) | 4.19 s |
| 00:46:45 | 35.65 | 948.11 (1291 s before fault) | 3.70 s |

Each starts with deviceState exceeding its five-second receive-age limit,
followed by managerState failing frequency checks. deviceState resumes with bad
frequency, and then both recover. The first publisher gap exceeds eight seconds.
All four start enabled and recover with enabled false. The last two establish
that a recent gap-button action is not required. `alertDebug` and `modelDataV2SP`
are ignored services in the original broad swaglog dump, not new failed required
publishers. Relevant swaglogs: 159368, 159370, 159383, 159395.

The feedback inbox contains one new steering-bite report from 3.7.6
(`66cf7cb5106045a481d70729bba88ee9`), not a 3.7.7 communication report. It is not
marked fixed by this change. Earlier steering reports remain separate work.

## Blocking mechanism and fix

hardwared publishes deviceState. manager polls deviceState, so a blocked
hardwared loop also lowers managerState's publication frequency. hardwared
called `set_offroad_alert("Offroad_TiciSupport", false)` every 0.5 seconds.
That calls native Params.remove, which takes `/data/params/.lock` even when the
alert file does not exist. The temperature alert cache also re-cleared a hidden
alert whenever the formatted temperature changed. Engagement metadata, uptime,
and status-packet writes performed synchronous Params persistence in this loop.
Concurrent Params writers hold the same lock while syncing the directory.

A 25-second offroad strace of the original hardwared recorded 75 publisher-thread
flock calls, 176 flock calls in total and 202 fsync calls across its threads.
It continuously rewrote NetworkMetered/GithubRunnerSufficientVoltage even when
unchanged, in addition to battery persistence. The trace confirms the dependency;
it does not establish which historical write or storage condition caused each
long driving stall. 3.7.7 fixed a separate follow-distance readback race but left
this shared-lock dependency. We cannot attribute the increased frequency to a
particular 3.7.7 change from these records alone.

3.7.8 sends hardware status persistence to one daemon worker with a fixed set of
nine permitted keys. Pending values coalesce by key; an indefinitely blocked
disk cannot create unbounded threads or queues. Identical status values are
ignored, and the worker checks existing values before any mutation. It verifies
readback because native Params discards write return codes, retries failures,
and never replaces a newer queued value with a failed older write. Payloads are
copied before enqueueing. Slow completions and first failures are logged from
the worker. PowerMonitoring shares this worker for its battery checkpoint.

The publisher still reads its inputs and computes thermal/startup/shutdown
conditions as before. Only status/telemetry persistence is deferred. Explicit
OnroadCycleRequested acknowledgement and DoShutdown commands preserve their
synchronous ordering; the worker rejects command keys. No service-health limits,
control algorithms, reader lists, schema, or takeover alerts change. Disk-backed
status is eventually persisted and can lag during storage trouble; it is not
used as a replacement for live control state.

## Verification

- Host: 23 targeted writer, follow-distance, fault-history and async-diagnostic
  tests, plus 256 feedback/link/HUD/reader-budget/annotation/marker checks.
- Device: native Params and the production hardware_thread, with isolated
  temporary storage and fixture hardware/message transport. Holding the private
  Params lock stalls the unmodified 3.7.7 loop (the regression test fails). The
  fixed loop continues publishing while persistence is blocked. Thermal danger
  and requested offroad mode still prevent starting under that same fault.
  No test subscribes to live deviceState/carState or writes live vehicle Params.
- Device: 29 existing power-monitoring, fan-controller and alert-manager tests
  pass with native Params redirected into a separate temporary directory per test.
- Ruff and diff whitespace checks pass for changed Python files.

These checks establish the reproduced lock-stall fix, not vehicle validation or
proof that every possible source of a communication alert has been eliminated.
The bounded process-health history remains enabled for subsequent real drives.
Installation must be verified independently using version/commit, manager and UI
health, port 8888, and post-restart publication timing; a Git push alone is not
installation evidence.
