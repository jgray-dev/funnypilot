# 3.7.8 minimap reliability update — September 18

The owner explicitly requested installation after the
[SSH investigation](minimap-cpu-starvation-20260918.md) found CPU 4 workqueue
starvation coinciding with map publication stalls. Communication takeovers had
ceased with the earlier hardwared fix; that fix and the existing fault checks
are retained. This update stays on `funnypilot-3.7.8`.

## Changes

`selfdrived` keeps its existing FIFO priority and carState-driven pacing but
runs on CPUs 0–3. Recorded card/controlsd/selfdrived work consumed essentially
all of CPU 4; removing selfdrived's approximately 22% share gives card/controlsd
and kernel workers headroom. The recorded housekeeping cores had spare capacity.
CPU 6 remains reserved for the non-real-time camera process and CPU 7 for model
work. No global scheduler sysctl, kernel setting, or controller algorithm changes.

The card/selfdrived Params threads and asynchronous diagnostic writers switch
to ordinary scheduling and housekeeping affinity from inside their own thread.
The parent control thread's settings are untouched. This fixes the case where
demoting a diagnostic writer alone left it pinned to an unavailable control core.

The map manager now owns one maintenance worker for durable Params, directory
operations and map housekeeping. Initialization writes also run there. Native
Params uses its existing atomic persistence and locking; they are not weakened.
Alert state is coalesced and verified, and an absent alert is not repeatedly
removed. A blocked worker cannot accumulate more workers or queued jobs.

The live map loop does not perform that housekeeping or load a previous drive's
disk-backed location. It resumes at the current time after a delay instead of
bursting through missed one-second ticks. Freshness uses consumer-monotonic
receive timestamps, with a two-second localizer limit. Invalid, stale or missing
GPS/localizer input does not refresh the shared position or mark speed limits
valid. A fresh fix restores normal publication.

The minimap expires a shared position older than three seconds using filesystem
wall time, and resumes drawing when a fresh position arrives. It logs the reason
instead of adding a banner. This check detects a stopped producer even though
the shared-memory file is still readable. It is not a new freshness guarantee
for every internal output of the native map-matching binary.

## Verification before installation

- Host targeted scheduling, asynchronous triage, HUD import/logic, annotation,
  and reader-budget checks: 221 tests pass after installing missing host test
  dependencies. The final mtime-read ordering change passes its focused test.
- Host feedback/Astra, diagnostic markers, hardware persistence and follow-
  distance regression group: 224 tests pass (some reader checks overlap above).
- Native device tests of candidate production modules with isolated transport
  and private temporary Params: **17 pass**. Holding the private disk Params
  lock blocks maintenance while map publication continues at one-second
  intervals. Tests also cover stale/missing/invalid localizer data, recovery,
  unchanged alert suppression, post-stall cadence, and the selfdrived entrypoint.
- The native thread-affinity test widens a restricted worker to CPUs 0–3,
  verifies ordinary scheduling, and verifies the parent's affinity is unchanged.
- 1,200 localizer samples from a recorded normal driving minute meet the new
  source-validity requirements. This is compatibility evidence, not a new drive.
- Ruff and whitespace checks pass. No reader, native schema, Params-key, or
  compiled-code changes are introduced.

Candidate tests ran outside `/data/openpilot` before installation. They neither
subscribed to live carState/deviceState nor locked or mutated live vehicle Params.

## Deployment and validation limits

The pre-update device revision is
`e5fb08e0903d310181366c3e85749ad0201e63c9`, to be retained as
`rollback/funnypilot-3.7.8-before-minimap-fix`. Installation is authorized by the
owner's request. Verify the exact installed commit, clean checkout, fresh boot,
manager/UI health and port 8888 after restart.

The tests establish storage-lock isolation and the intended scheduling/freshness
behavior. They do not measure the new onroad CPU distribution or prove that all
future map freezes are eliminated. Those require post-install drive telemetry,
especially CPU 4 idle/worker progress, control timing and map publication gaps.
