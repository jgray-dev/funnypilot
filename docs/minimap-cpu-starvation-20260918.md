# 3.7.8 minimap investigation: CPU 4 workqueue starvation

## Scope and identity

On September 18 the owner restored local SSH access and confirmed that the
remaining symptom is minimap freezing. Communication/takeover alerts have
ceased. This investigation used read-only SSH commands, kernel/debugfs counters,
and downloads of existing recordings. No process was restarted or attached to
with a debugger; no scheduler, device setting, or running code was changed.

The device was offroad and clean at
`e5fb08e0903d310181366c3e85749ad0201e63c9`, branch `funnypilot-3.7.8`.
The inspected recordings identify the same commit. That commit contains the
3.7.8 runtime fix plus the previous investigation notes; it does not contain
a map-stall repair. Kernel: `4.9.103 #3 SMP PREEMPT`, built December 18, 2025.

Private evidence, including location-bearing telemetry, is retained outside Git
at `~/.local/state/funnypilot/investigations/2026-09-18-minimap-378/`.
Only findings belong in this document. The feedback inbox contained two steering
reports, neither a minimap report; their review states were not changed.

## Main finding

The previous investigation established a storage stall but could not identify
why storage stopped progressing. Kernel records now show **CPU 4 workqueue
starvation**, including block-device work, at the same time as map publication
gaps. Recorded process CPU use and source affinity settings identify an
overloaded real-time core as the leading cause.

- The retrieved kernel ring contains **254 workqueue-lockup warnings, all for
  CPU 4**. The largest reported wait is **496 seconds**. These are repeated
  watchdog reports, not 254 independent incidents.
- Both ordinary and high-priority kernel worker pools are affected. Pending
  storage work includes `cfq_kick_queue`, `scsi_requeue_run_queue`, and
  `blk_timeout_work`. A high nice priority does not make these FIFO real-time
  workers.
- `card`, `controlsd`, and `selfdrived` all explicitly select CPU 4 and
  `SCHED_FIFO` priority 53. Their recorded aggregate process CPU time consumes
  essentially one entire core. In four 30-second samples from the evening
  drive: card uses 49–51%, controlsd 27–29%, and selfdrived 21–22%. CPU 4 records
  approximately 99.4–99.5% user/system time and no measurable idle time.
- Other cores have substantial idle time in those samples. Total-device CPU
  averages therefore hide this bottleneck.

The read-only checks found `sched_rt_runtime_us=950000` and
`sched_rt_period_us=1000000`; **RT throttling is not globally disabled**. The
observed starvation must not be explained by an invented `-1` setting. The
kernel has RT group scheduling enabled and uses cgroup v2. This investigation
does not establish the exact runtime-budget sharing behavior of the deployed
vendor kernel. The kernel's actual worker starvation and measured workload are
the evidence, independently of an assumed effective per-core reserve.

Linux documents the runtime-budget controls and the need to reserve execution
time for non-real-time work in its
[real-time scheduling documentation](https://www.kernel.org/doc/html/v6.0/scheduler/sched-rt-group.html).
Changing the global RT budget without measuring control deadlines is not a
validated repair.

## Direct overlap with a map stall

Morning route `00000465--d5999a6055`, segments 17, 18, 21, and 22, supplies the
following evidence. Times are UTC on September 17; subtract four hours for EDT.

| Time | Finding |
| --- | --- |
| 13:00:46.954–13:06:59.099 | A **372.145-second gap** between map publications, followed by rapid catch-up publications. |
| Same interval | GPS continues at about 1 Hz; all 379 recovered GPS samples in segment 18 show motion. Maximum GPS interval is 1.198 seconds. |
| Monotonic 48149.127 | Map manager, logger, filesystem journal, and other services are in uninterruptible kernel sleep (state D). |
| Kernel monotonic 48121–48483 | Repeated CPU 4 ordinary/high-priority workqueue-lockup reports, with pending block timeout work throughout the map gap. |
| 13:09:41.955–13:38:50.712 | A second, approximately **29-minute gap in recorded map publication**, followed by a large catch-up burst. Recovered GPS continues across this interval. |

The second duration describes publisher timestamps, not a separately measured
screen-freeze duration. It must not be presented as the owner's exact observed
duration. Logger stalls lose or overwrite some queued telemetry, so absent
control records are not proof of stopped control processes. In particular,
recovered `deviceState` in segment 18 covers only part of the long stall because
the queue is bounded.

In evening route `00000468--23e6ef0404`, sampled segments 4 and 12 have regular
map/GPS/heartbeat publications even while CPU 4 is saturated and kernel workqueue
warnings recur. CPU starvation is an ongoing condition; a visible map stall
also depends on which pending work or filesystem operation the map pipeline
needs at that moment. This explains why the symptom can be intermittent.

The current process-health file contains 66 transitions dated September 17 or
later, all with `enabled=false` and speed below 2 m/s. They are consistent with
startup/shutdown transitions, not evidence of a new moving communication
takeover. This agrees with the owner's clarification; it is not a guarantee
against every unrecorded fault.

## Storage findings and limits

The UFS controller's debug counters report no errors causing controller reset,
no other recorded UFS errors, and zero reported physical/link error counters.
Current outstanding requests and error flags are zero. Standard SMART is not
supported by this UFS device, so no SMART health conclusion is available.
About 11 GiB is available on `/data` (88% used at inspection).

The evidence favors delayed scheduling of storage work over failing flash as
the explanation for the observed stalls. It does not certify the flash or
exclude every independent storage issue. An unrelated polkit process hit its
own 50 MiB cgroup memory limit; that is not evidence of a system-wide out-of-
memory kill of the map or control services.

## Repair implications

The causal chain supported by the evidence is:

1. Real-time workload consumes the CPU shared with CPU-bound kernel workers.
2. Pending storage work cannot make timely progress.
3. Filesystem operations and journal-dependent writes wait.
4. The map manager, which performs synchronous disk housekeeping/alert
   persistence before its map tick, stops updating shared position/map data.
5. The minimap retains old position/geometry and automatic speed-limit updates
   stall. SCC can still use independently received GPS and cached geometry.

3.7.8 moved hardware status persistence out of the heartbeat loop, explaining
why communication alerts can remain resolved while map stalls persist.

A complete repair needs measured CPU headroom on core 4 (reduce or redistribute
work after auditing deadlines and thread affinity), plus separation of map
housekeeping/persistence from publication and explicit underlying-data freshness.
Simply moving one Params write to a thread does not resolve kernel starvation.

There is also an observability weakness: `AsyncTriageRecorder` lowers its
worker's scheduling class but does not move it off the creator's CPU affinity.
For callers pinned to CPU 4, the worker can be starved there. Future diagnostics
must have both an appropriate scheduling class and access to a suitable CPU,
without adding IPC subscribers or blocking control loops.

No runtime change was made or claimed validated by this investigation. A repair
must verify control timing, worker progress, and fresh map updates under load;
an offroad configuration change alone cannot establish those outcomes.
