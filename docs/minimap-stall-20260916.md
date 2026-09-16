# 3.7.8 minimap/SLA stall — September 16 investigation

## Scope and identity

The owner reported an approximately four-minute minimap freeze around 09:40
local time, with SLA retaining 35 mph while SCC continued slowing for corners.
No feedback report was saved. Ordinary route telemetry and process logs were
retrieved through Astra's bounded, read-only file API; no SSH, device command,
installation, settings change, or restart was performed.

Fresh device identity and the recordings both identify clean 3.7.8 commit
`d6831f270b220e1dfe3ee0d2245c6a4d1fcbd2e0`. The investigated recording is
`0000045c--77d2e1253f`, segments 10–13. Downloads were assembled from bounded
chunks with consistent file size and no reported redaction or concurrent
change. The device subsequently went offline; segment 14 could not be read.

Private evidence is outside Git at
`~/.local/state/funnypilot/investigations/2026-09-16-minimap-378/`.
It includes qlogs, selected decoded telemetry, chunk provenance, and sampled
swaglogs. It contains location information and must remain private. The feedback
inbox had no report for this episode; no existing steering report was marked
fixed or otherwise repurposed as evidence for this incident.

## Confirmed findings

Times below are UTC. Subtract four hours for EDT; this places the incident in
the owner's reported 09:40 window. Recorded `clocks.wallTimeNanos` establishes
the conversion from telemetry monotonic timestamps.

| Time | Evidence |
| --- | --- |
| Before 13:40:43 | Map messages arrive approximately once per second. The recorded limit changes from 45 to 35 mph before the stall. |
| About 13:40:44 | Segment 11 stops accumulating normal telemetry after about 1.5 seconds. The lateral diagnostic file also stops near this time. |
| 13:40:50.248 | Last map message before a long publication gap; limit is 35 mph. |
| 13:41:12 and 13:41:42 | `procLog` records the map manager and logger in Linux state D (uninterruptible sleep). The filesystem journal process `jbd2/sda12-8`, feedback recorder, and several other services are also in D. |
| 13:43:54.147 | Map messages briefly resume after **183.898 seconds**, in a rapid catch-up burst, still reporting 35 mph. |
| 13:43:54 onward | Logger rotates late, reports old-segment encoder packets, then repeatedly drops frames because encoder queues are too large. Sampled warnings continue through 13:51. |

This is evidence of a shared storage/I/O stall, not merely a stalled map drawing
routine. The map manager and logger retain the same PIDs, use essentially no
CPU during the sampled stall, and are blocked in kernel waits. Aggregate CPU
I/O-wait counters rise sharply: 59.83 cumulative CPU-seconds before the event,
113.29 thirty seconds later, and 183.51 another thirty seconds later. These are
summed CPU counters, not elapsed stall duration.

The logger eventually drains some queued telemetry into segment 12. That
recovered data shows 193 raw GPS samples around the stall, all with distinct
positions and a maximum receive interval of about 1.13 seconds. The same window
contains 386 `deviceState` messages with a maximum interval of 0.505 seconds.
GPS input and the hardware status publisher therefore continued while the map
publisher stalled. A logger gap alone would not establish this distinction.

The device had about 34.6% free space; recorded temperatures stayed in the green
thermal state. Neither a full disk nor thermal shutdown explains this event.
This does not establish that the storage hardware is healthy.

Segment 13 contains another 72 seconds of other telemetry without recorded map
messages. Thus 183.898 seconds is a measured publication gap, **not** proof that
the complete user-visible freeze lasted exactly that long or fully recovered at
13:43:54. The recovered trace does not establish the precise final recovery time.

## Why map, SLA, and SCC behave differently

`sunnypilot/mapd/mapd_manager.py` performs filesystem housekeeping and calls
`set_offroad_alert()` before each `live_map_sp.tick()`. That alert call performs
synchronous durable Params writes/removals; even removing an absent alert takes
the global Params lock. Filesystem access, lock contention, and persistence are
therefore on the live map publication path.

The tick updates `LastGPSPosition` in shared memory and publishes
`liveMapDataSP`. The minimap reads that position and the map geometry from shared
memory. If the map manager blocks before its tick, the UI can keep drawing the
previous values. Its polling code does not expire that position by age.

SLA consumes `liveMapDataSP`. Its resolver already rejects messages older than
two seconds, but maintains a last-known speed limit separately. In addition,
the map producer checks the localizer's stored `gpsOK` flag without checking
whether that localizer message is fresh. A newly published envelope can
therefore contain stale underlying information. These are distinct freshness
issues; removing the existing two-second check would worsen them.

SCC's map controller reads raw GPS through the planner's existing subscriptions
and can locate the vehicle on cached road geometry independently of
`LastGPSPosition`. Its vision path is also separate. Continuing corner slowing
is consequently compatible with a frozen minimap and SLA feed. The recording
gap prevents attributing each reported corner intervention to a particular SCC
source; no such attribution is claimed here.

## Relationship to the communication fix

3.7.8 isolated hardware-status persistence from `deviceState` publication. The
continuous hardware heartbeat during this episode is consistent with that fix
working. It did not remove synchronous disk dependencies from the map manager,
logger, or all other background services, nor repair the underlying source of
long storage waits. This incident exposes that remaining scope.

Do not equate missing high-rate control records during the logger stall with a
control-process outage. Conversely, continuous `deviceState` is not proof that
every control service remained healthy throughout the missing interval.

## Limits and next repair work

The evidence establishes the I/O stall and the map pipeline's vulnerability to
it. It does **not** identify the initiating syscall, lock owner, filesystem
transaction, storage-driver fault, or failing hardware component. Process state
D is not a stack trace. Native mapd output is also redirected to `/dev/null`,
which removes potentially useful diagnostics.

The next repair should:

1. Separate map publication from durable alert persistence and map housekeeping,
   using bounded workers and coalesced status updates. Audit remaining disk
   reads too; moving only one write cannot guarantee immunity to storage stalls.
2. Carry and check underlying location/map freshness through the shared-memory
   path, preserve SLA's existing message-age check, and distinguish cached
   limits from current ones. Do not introduce unsolicited on-screen banners.
3. Add bounded diagnostics for map tick delays and slow storage operations,
   retaining data in memory during stalls rather than depending exclusively on
   the blocked disk. Avoid new carState/deviceState subscriptions.
4. Inspect kernel/storage diagnostics and device health when access permits to
   distinguish a driver/device timeout from filesystem/write contention. Audit
   logger recovery separately: resumed processing did not end the frame drops.

No runtime fix or new release was installed in this investigation. These are
evidence-based repair directions, not a claim that the remaining issue is fixed.
