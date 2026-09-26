# 3.7.9: feedback, steering handover and storage reliability

Based on clean 3.7.8 `d188a6a60cf3096839357f038ebe05a07ec4217c`.
GitHub source release only; no device install/restart in this task. Home SSH
was unavailable. No new IPC subscriptions, schema fields, Params keys, or
compiled changes. The 3.7.8 communication and minimap fixes remain included.

## Display and report interaction

The upcoming-zone outline now fills clockwise from twelve o'clock, with fixed
5-pixel thickness and a faint empty track. It follows the sign perimeter (round
for the metric sign, rounded plate for the imperial sign). Red/green retain
the lower/higher-zone meanings. Progress is `clamp(1 - distance / 400, 0, 1)`;
confirmation state cannot pulse or ease it ahead of the reported boundary.
The HUD projects fresh map distance using current speed and consumer receive
age, as the resolver does. After two seconds without valid map data it hides
the upcoming preview. Completion refers to the mapped boundary, subject to
GPS/map accuracy, not an independently detected physical roadside sign.

The open report popup is 1020×510, with 480×100 label targets and a 150×72 Done
target at the nominal 1920×1080 viewport. The closed Report button remains
150×65. Multiple labels, acknowledgement/retry and alert priority are retained.

## Post-blinker handover

The old release gate accepted a wheel within 20 degrees of centre for 0.67 s,
without examining model output. The new gate evaluates distinct, fresh model
frames, not repeated 100 Hz copies of one prediction:

- The original low-speed single-blinker pause trigger remains. Once paused,
  continuing signal/hazards or a driver steering press restarts the dwell;
  accelerating above the trigger speed cannot prematurely end the pause.
- Require at least 0.67 s of consistent model output, or the user's longer
  configured delay (including 10 s). Missing/invalid/stale data resets the dwell.
- Check model action and yaw-rate predictions at 0, 0.5 and 1 s in lateral
  acceleration units. Across the entire dwell, each component must stay within
  0.35 m/s². The model must not be in a lane-change maneuver.
- Action must agree with current measured curvature within 0.5 m/s²; prediction
  spread must also be at most 0.5 m/s² and magnitude at most 2 m/s². Below 2 m/s,
  defer automatic handover until motion estimates are more useful.
- A stable curved path can pass; steering angle itself has no straightness
  requirement. Existing engagement/fault gates and the blinker-specific
  15%-to-100% torque ramp over 3 s remain.

These are conservative handover thresholds, not road-validated tuning. Replay
of 5,588 real model frames from the two recent bite windows accepted 3,760
settled frames. Neither recording had an accepted frame with wheel angle over
20 degrees; that new capability is tested synthetically and still needs vehicle
validation. Feedback now records pause state and the number of settling frames.

## Repeated steering bite: evidence and bounded mitigation

All nine new reports were downloaded with whole-file and part checksums
verified. Recorded version, clean/dirty identity, telemetry, triage and qlogs
were reviewed; road-video context was inspected wherever present. Full rlogs
from the two most recent bite reports supplied actual car parameters and live
vehicle-model calibration for replay. None of this proves closed-loop vehicle
improvement on 3.7.9.

Reports `8632e1bea68846b8beb1a1636f210386` (September 22) and
`9afda8f018314e6499673e40451bef86` (September 24) are clean `d188a6a60` recordings.
In each report's -7 to -3 second window:

- Wheel angle repeatedly rises/falls while model desired lateral acceleration
  is substantially smoother. The September 22 window spans 16.4–28.1 degrees;
  September 24 spans -23.8–-11.9 degrees. These are window extrema, not one
  oscillation's amplitude.
- No `steeringPressed` samples occur, override scale remains 1, and pitch stays
  below the 5 deg/s bump trigger. September 22 has no EPS driver-limit clamp;
  September 24 has a few. Driver override and bumps do not explain the repeated
  oscillations in these selected windows.
- Requested/applied torque mismatch is flagged in approximately 91% and 94%
  of samples. That flag is computed from request/output divergence, which also
  includes ordinary slew/transport lag; it is not proof of a Panda violation.
- The motion-credit correction was explicitly disabled by that flag, so the
  controller usually stopped accounting for demonstrated wheel travel already
  closing the error. Friction correction also alternates during the oscillation.

Change: remove only this request/output-mismatch exclusion from the existing
bounded motion-credit eligibility check. The same flag still freezes the PID
integrator. Driver press/handback, actual EPS driver limiting, bumps and curvature
limits still exclude credit. Panda/CarController limits, EPS governor, controller
gains, model knots/timing, feedforward path and credit limits are unchanged.
Credit still requires wheel motion toward the target beyond already-planned
reference travel, is at most 0.12 m/s², and cannot reduce error correction below
35%. It cannot reverse the tracking-error sign.

Counterfactual replay using recorded wheel rate, vehicle parameters, reference
travel and existing exclusions restored damping in 67/338 and 102/288 distinct
sampled frames, versus 5/338 and 13/288 with the old exclusion. Maximum credit
was 0.0782 and 0.0882 m/s², below the unchanged bound. This evaluates the changed
eligibility logic on recorded motion; it does **not** simulate how the physical
wheel would move after different commands. Reports remain triaged, not fixed.

## Feedback inbox organization

| Report | Recorded build | Category and disposition |
| --- | --- | --- |
| `9afda8f018314e6499673e40451bef86` | clean 3.7.8 `d188a6a60` | Repeated corner bite; detailed replay above. Candidate mitigation, drive validation pending. |
| `8632e1bea68846b8beb1a1636f210386` | clean 3.7.8 `d188a6a60` | Repeated corner bite; detailed replay above. Candidate mitigation, drive validation pending. |
| `5bf0360408c24801aa8499608d5bee9d` | clean 3.7.8 `d188a6a60` | Bite on a winding road; includes lateral inactive periods and a steer-saturation warning. Do not assume every event shares the same mechanism. |
| `5929d01b38694548a9942135767c1e5a` | clean 3.7.8 `d6831f270` | Older bite evidence predating the CPU/map update; telemetry/rlogs present, all three road-video segments missing in manifest. |
| `66cf7cb5106045a481d70729bba88ee9` | clean 3.7.6 `0cc713db1` | Historical steering bite; different controller/runtime context, retained for comparison. |
| `550faa99b7a142ec8b61ac4e7016fe0f` | clean 3.7.8 `d188a6a60` | Steering wander; lateral active throughout, some driver-override activity. Remains a separate open symptom. |
| `4e72c8492aa94ad1be64ea84fa773abe` | clean 3.7.8 `d188a6a60` | Unnecessary slowdown, daytime lead-vehicle scene. No eligible governing corner frozen at report time; no automatic corner relaxation. |
| `4ba720680dab4d30b6a30220dbc310ce` | clean 3.7.8 `d188a6a60` | Unnecessary slowdown, wet night scene. Captured corner explicitly not governing; no automatic corner relaxation. |
| `64fa881bfb8840e2a5cf0f96d0073e8b` | clean 3.7.8 `d188a6a60` | Late braking with a lead at night; includes longitudinal override/manual-longitudinal alert. No removal of lead/stop constraints; remains open for dedicated investigation. |

Older `67ddb42b34f043d9a30d66251935cf3b` and `9599da1d517f425aa498bf6abb000920`
retain their existing triage; their prior notes do not establish a fixed symptom.
Private recordings and screenshots are not checked into Git.

## Clip retention and full-storage recovery

Reports previously called `set_route_saved(True)`, permanently protecting the
entire drive, including subsequent segments. Cleanup correctly respected those
saves, which meant reporting could progressively reduce reclaimable space.
3.7.9 instead pins only the surrounding three minute segments under the shared
save/deletion lock. A background worker retains the closed files via hardlinks,
persists clip metadata, then releases pins. Logger locks delay finalization.
Original files are not rewritten/transcoded. Forty seconds of pre/post telemetry
remain; whole minute files preserve playable video and complete compressed logs.

Pins survive restart and disk-write failures. Manual deletion and automatic
cleanup both honor them. Uploads use retained clip files, independent of original
drive lifetime. Cloud acknowledgement permits removal of local road-artifact
links; queued/unacknowledged clips survive. Explicit saves are never altered.
Legacy saved flags lack provenance, so older automatically saved drives cannot
be safely distinguished from explicit saves and are not silently unsaved.

Deleter checks pressure each second, begins below 15% free or 5 GiB, and drains
until 20% and 7 GiB are available. One segment per 100 ms bounds work. Normal age
scans remain once a minute. Corrupt retention metadata pauses deletion; saved,
recording and pinned segments remain exclusions. Protected data can still fill
a disk; there is no promise of free space if all remaining data is protected.

The owner dates the full-storage event to Tuesday afternoon. The September 22
16:11 UTC report shows 9.65–10.05% free; the 21:14 UTC report shows 35.2–35.8%.
Most other recent clips hover near 10% free. None of the available report windows
contains the reported full-storage alert, so these observations establish weak
headroom and a retention problem, not the exact Tuesday failure sequence.

## Isolated GPS-unavailable event

No timestamp or matching failure traceback was available. Sampled qlogs contain
GPS publications and no matching qcomgpsd/storage failure. Do not claim a proven
root cause. A code-level recovery defect is independently demonstrable:
`ModemDiag.recv()` formerly waited forever in `select()` for a packet delimiter,
and command-response waiting could consume unrelated logs forever.

Receive now has a 10-second absolute deadline, EOF handling and a 256 KiB frame
bound. Command-response waiting shares an absolute deadline across unrelated
packets. Modem subprocess calls are bounded; assistance downloading cannot block
process exit as a non-daemon child. A failed receiver can exit and be restarted
by existing manager supervision. No automatic device reboot or modem firmware
change is introduced; loss of satellite fix alone does not trigger this timeout.

## Verification

The combined host regression run passed **559 tests**; Ruff and `git diff
--check` passed. The three pytest warnings concern optional host plugin config.

- Production-controller tests exercise both signs, neural/non-neural paths,
  request/output mismatch, unchanged feedforward, bounded credit and integral
  freeze. Model-settle tests cover curved handover, driver/signal re-entry,
  stale/duplicate/malformed predictions, drift, gaps and configured dwell.
- Temporary-filesystem tests cover pressure hysteresis, explicit saves,
  saved/deletion serialization, clip pins, recording locks, ENOSPC/retry,
  original deletion before upload, lost cloud acknowledgement, unlink failures,
  and corrupt retention protection.
- GPS fault injection covers silence, EOF, oversized/fragmented frames,
  buffered packets and unrelated-log floods without touching a modem.
- UI tests cover clockwise positions, exact distance progress, stale projection,
  popup targets, label retries, alert priority, import isolation and annotations.
- Reader census and diagnostics marker checks remain required.

Vehicle/modem hardware validation and native device startup remain pending;
this release is pushed to GitHub for the owner to install.
