# Astra / Comma communication and driving-feedback investigation

Owner request: remove ignition/gear restrictions on off-road maintenance, make
Astra cooperate with the Comma, and investigate reported corner chatter and wander.

## Identity and live checks

The initial local checkout was clean `funnypilot-3.7.4`
`c2408e537f1376bcb6610336b82b07037529614a`. The device and both reports were clean
`funnypilot-3.7.5`, `d8bec23cbe50f54e7f4aa53a94cc7078c6d3ab25`.
Fetched 3.7.5; at the owner's request, created `funnypilot-3.7.6` from that exact
commit with the changes. The original 3.7.5 remains unchanged for rollback.
SSH worked at the owner-supplied address. Feedback and Astra daemons were running.
A direct native read showed IsOnroad=false, IsOffroad=true, Panda noOutput and
controlsAllowed=false. Cloud heartbeat nevertheless had stationary=null and
engaged=null, which the old policy interpreted as a permanent maintenance denial.
A read through the production Worker/device path returned the real version in
13.0 seconds. This measured the old idle poll/result delay, not an SSH failure.

## Fixed communication defects

1. Native mode was inferred from ignition; commands required onroad-only motion
   and engagement publishers even offroad. Now offroad requires both manager
   flags and fresh disabled Panda output. Ignition, gear and motion are irrelevant
   in that state. The prior stationary/disengaged path remains available.
2. The prompt and adapter repeatedly demanded a separate browser approval for
   every exact command. Owner-authorized requests now use the authenticated,
   device-bound session capability. Legacy browser proposals still work.
3. Execution leases compared checkout identity throughout an edit, so dirtying
   the checkout or committing could kill the running command. Identity remains
   bound before spawn; execution reports its actual changing identity while
   state, revocation, timeouts and lease expiration remain enforced.
4. Completed results waited for the next poll; idle polls were 15 seconds apart.
   Results now post immediately and idle polls run every 5 seconds. Commands
   invalidate the cached identity. Useful curated errors replace generic refusal.
5. The receipt journal permanently filled after 256 commands. New receipts carry
   expiry and may be pruned after expired requests become ineligible for dispatch.
   Legacy receipts lacking timestamps are not guessed old or evicted.
6. Astra had no explicit host-cloud feedback tools. Linked sessions now expose
   private list/download/review operations, with no vehicle-state dependency.
   Downloader skips verified files and atomically publishes newly verified files.

Authentication, private credentials, scoped device binding, no replay, process
supervision, and vehicle control protections remain in effect.

## Report 67ddb42b34f043d9a30d66251935cf3b — steering bite/chatter

Cloud bundle checksums verified: 2,381 telemetry samples over 39.98 seconds,
40 triage rows, surrounding road video and qlogs; no manifest artifacts missing.
Retrieved preserved segment 2 full rlog over SSH (about 12 MiB compressed) and
parsed its real cereal events. Road video shows the winding road at night.
All recording identity fields match clean 3.7.5 above.

The full log confirms wheel oscillation during monotonic 182–185 s, preceding
report time 189.9667 s. Wheel-angle maxima near 183.00, 183.61, 184.31 s were
19.1°, 20.9°, 21.1°; intervening minima near 183.26 and 183.93 s were 15.2°.
The repeated excursion is about 4–6° with a 0.6–0.7 s period. Model desired
lateral acceleration is substantially smoother, while requested and applied
torque both oscillate. This places the observed amplification in the torque
feedback/vehicle response, not merely noisy model curvature knots.

`steeringPressed` is false and override scale is 1.0 throughout this capture.
That falsifies driver-override softening as the explanation for this event.
The controller's P and friction-containing feedforward terms oscillate; motion
credit and steering-limit flags also vary. The existing recording does not retain
all effective controller inputs and per-frame internal transitions needed to
separate those coupled mechanisms. Do not call a gain or friction retune verified
from this plot, or claim that the separate MADS fix cures this corner.

No speculative gains, curvature filtering, EPS limit increases, or SCC/lead/stop
constraint changes were made. Status remains triaged, not fixed.

## Report 1737ed22cff941cebfb2288920da42ee — steering wander

Cloud bundle verified: 2,269 samples over 39.98 seconds, 40 triage rows and road
video/qlogs. Retrieved segment 24 full rlog. Recording identity matches above.

Confirmed code defect: `Controls.publish` updated `steer_limited_by_safety` only
when `selfdriveState.active`, although MADS can keep CC.latActive=true while
selfdriveState is disabled. The previous flag then persisted indefinitely.
That flag freezes the torque integrator and disables SteeringMotionCredit.
The report records slb=1 throughout, motion scale=1, lateral-only driving and a
zero integrator. During 38106–38120 s, approximately 90% of full-log command/output
samples agree within .01 normalized torque, contradicting a continuously limiting
actuator. Timestamp interpolation here is an estimate, not a CAN ground truth.

Fix: update on CC.latActive and clear when lateral control is inactive.
Production-block regression tests verify clearing a stale flag, detecting real
limits with MADS, inactive reset and ordinary full engagement. This fixes a
specific evidenced stale-state bug. Vehicle validation must establish how much
of the owner's perceived wander it resolves; report stays triaged with fix hash.

## Expanded future evidence

40-second pre/post window; up to 100 Hz recorder sampling (old .01-second
wall-time throttle dropped many nominal 100 Hz frames). Bounded 4,500-sample
memory ring, at most three simultaneous events. Effective torque-controller
friction/error/tuning terms, model/interpolated/commanded curvature, roll/offset,
delay, signed wheel rate, motion credit, bump/override/lane-change scales, and
applied actuator output go through /dev/shm. Original publish/receive timestamps
and validity accompany every copied service. No added IPC subscribers or capnp
schema changes. Full rlogs join the surrounding video/qlogs; 4 MiB checksummed
parts, 128 MiB per file and 512 MiB per event. Logger excludes DONT_LOG Params;
no cabin video/audio files are selected. Uploads remain in the offroad Wi-Fi
worker and preserve unacknowledged events and saved routes.

## Validation and activation

- Focused Python suite: 263 passed (link, capture/faults, reader census, controller
  guard, EPS, handback, bump damper and knot-filter regression tests).
- Final isolated Astra suite: 53 passed, including the real Python/MCP/local
  Worker integration and HTTP command lifecycle; no inference or live devices.
- TypeScript checks and both Worker deployment dry runs passed; Python Ruff clean.
  Two downloader resume/checksum-failure tests and 23 annotation/diagnostic-marker
  checks passed. Changed controller modules imported successfully using the actual
  Comma native runtime from a temporary review directory, without activating them.
- Broader Astra tests expose pre-existing agent-conversation failures; all three
  reproduced in the untouched source backup, as did the conversation parser and
  five model tests (including an absent pinned native binary). Full broad suite:
  155 passed, 20 failed, 2 cancelled; browser UI fixture timeouts remain outside
  the scoped device suite. They are not driving evidence.
- Native torque-buffer test depends on the complete native runtime; host import
  limitations must not be replaced by mocked native modules.

Cloud activation completed on 2026-09-14:
- Astra Worker `717f8d2a-7831-4741-9f18-e710af46cd48` at astra.jgray.cc.
- Feedback Worker `7a492e0f-05cd-495d-bd9a-55703e6dae49`.
- Astra broker/chat restarted only after confirming zero running PTYs, to load
  the new prompt and tools. On-device installation/activation remains pending
  explicit authorization; current vehicle software is still clean 3.7.5. Source changes, Git pushes,
cloud deployment, on-device installation, process activation, and vehicle
validation are separate states. No vehicle restart or installation was performed
as part of the diagnostic reads.
