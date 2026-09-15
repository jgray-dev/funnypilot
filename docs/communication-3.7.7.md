# 3.7.7 communication investigation — 2026-09-15

Branch `funnypilot-3.7.7` is based on clean 3.7.6
`0cc713db1ba65b849471497914e278f0a5d26e83`. 3.7.6 remains unchanged.
The owner reports repeatable communication soft-disables when changing follow
spacing behind a lead, plus brief audible communication faults during drives.

## Evidence and unresolved diagnosis

SSH at the supplied home address repeatedly timed out or returned no route to
host. The production Astra registry also reports the device offline; its last
identity was clean 3.7.6. No device installation, restart or live native test was
performed in this investigation.

Queried the private feedback inbox and downloaded report
`9599da1d517f425aa498bf6abb000920`, route `0000044b--16b57f2840`, verifying every
artifact checksum. Recording identity is clean 3.7.6 above. It is labeled
steering_bite, not a communication fault. Its 6,061 telemetry samples span
79.979 seconds; all selected control/plan/calibration/event streams are valid,
with 66 explicitly missing/stale optional control-tap samples. All 6,061 alert
types are empty. Reviewed its road video, triage and native full rlogs for
segments 8, 9 and 10 (roughly monotonic 518.5–698.5 s). There are no gap-button
edges, communication events or relevant tracebacks in that three-minute window.
All logged control, planner and localization messages inspected there are valid.

The orange-to-red behavior matches the existing communication soft-disable
state machine and escalation. It does not establish which publisher failed.
The code defects below are verified independently; neither their existence nor
passing local tests proves the owner's takeover or transient warnings cured.
The affected drive's full rlogs and device swaglogs are still needed. Preserve
all communication/validity thresholds and takeover behavior while diagnosing.

## Verified follow-distance race

3.7.6's main loop directly assigns the next personality on a gap-button release
and queues a nonblocking Params write. Its background Params thread independently
assigns the persisted value to the same field every 100 ms. A read begun before
the press, or completed before the queued write reaches disk, restores the old
selection. It can then change again when the write completes.

An isolated execution of the actual 3.7.6 button block and background assignment
(AST extracted from the committed source, with deliberately delayed storage)
reproduced `standard -> aggressive -> standard` after a single press. This is
proof of the setting race, not a reproduction of a vehicle communication fault.

`FollowDistanceSetting` now owns a revisioned setting and pending write. The main
loop cycles it without disk access; the Params worker serializes persistence and
checks readback. A read or write from an older revision cannot overwrite a newer
button request. Repeated presses during a slow write coalesce to the latest
selection. Invalid stored enums and unacknowledged writes retain the valid
requested value; the worker logs a persistence failure and retries. External
settings edits remain available after the pending button request is persisted.
Headway values, lead/stop constraints, MPC costs and torque gains are unchanged.

## Real-time IO and service evidence

`controlsd` and `radard` called the persistent JSONL recorder in their real-time
loops. Catching IO exceptions did not bound disk latency. They now use a daemon
writer with a 32-record queue, nonblocking enqueue and normal scheduler priority.
A blocked disk can drop diagnostic records (counted explicitly); it cannot hold
up the producer. Rotation and existing log names remain intact. Tests hold the
writer on a blocked disk, fill the queue, and verify the producer continues.
The duplicate `liveDelay` entry in controlsd's SubMaster was also removed.

`selfdrived` keeps its existing communication checks and cloudlog events. A new
bounded asynchronous `process_health.jsonl` additionally records failure changes
and recovery, exact checked services, validity/aliveness/frequency flags, receive
age, original publisher timestamp, episode duration, current follow selection,
lead state and the last gap-button edge. Ignored services are not misreported as
failures. This history is available in the web Logs page and included in matching
feedback triage windows. No additional IPC subscribers or schema changes.

Astra's manager flags had a separate 3.7.6 integration bug: this fork's
`Params.get` returns typed BOOL values, but the monitor compared them with raw
bytes. It now accepts only actual booleans and preserves unknown for missing or
invalid values. Fresh Panda noOutput/silent evidence is still required offroad.

## Alert presentation

Removed the UI-generated readiness/engagement fallback. An idle, unengageable
state no longer manufactures an "Engagement blocked" banner. The real state
machine still selects noEntry alerts when an attempted action is rejected;
process-loss watchdogs, calibration failures, unknown faults and takeovers show.

Explicit routine notice pairs are quiet: lane-change/turn progress, successful
personality/bookmark/audio-feedback status, ordinary startup/mode notices,
calibration progress, driver-requested manual-control transitions and reverse
status. Reverse refusal/disengagement and critical alerts still show. The
known startupMaster notice is quiet even though it uses userPrompt severity;
this is not a blanket severity filter. State-machine event streams, feedback
evidence, control gates and fault audio are unchanged.

## Validation and remaining work

- 270 focused tests passed for alerts, async fault injection, follow-distance
  concurrency, communication history, Astra link and feedback. One additional
  feedback-bundle test passed for process-health window inclusion (271 total).
- 31 annotation, diagnostic-version and reader-budget checks passed (some overlap
  with the focused feedback suite); Ruff and whitespace checks passed.
- Native controller/planner integration cannot run in this host checkout because
  the available compiled extensions are ARM device binaries. No mocked native
  modules were used to claim native integration. Changed pure Python components
  ran locally; native imports and affected-route replay remain pending access.
- Next: retrieve affected rlogs and swaglogs, correlate gap-button edges with the
  first invalid/stale/rate-limited service and process traceback/timing, and test
  the resulting causal fix. Do not mark the reported driving symptoms fixed from
  this robustness patch alone. Device installation/restart remains unrequested.
