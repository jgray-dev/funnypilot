# 3.7.12: bounded post-signal pause and remaining steering-bite damping gap

Based on clean 3.7.11 `f7e393810c8478969d7afd1722b82e92c05d4b8e`.
Source release only; no device installation/restart or physical vehicle
validation in this session. The communication, minimap, driver-guidance,
cruise-button and active-drive-download changes remain.

## New driving evidence

Report `0f9227c7bde0481e8f3b1942db5b618b`, labeled steering_bite, records
**clean 3.7.11 at the exact base commit**, not an older installation.
Downloaded artifacts passed the feedback tool's checksums; none are missing.
Reviewed its manifest, road-video samples, telemetry/controller samples and
full log segments together. Video shows a right-hand bend with a lead vehicle;
it cannot establish steering force or controller causation by itself.
CarParams identifies KIA_K5_2021, and controller taps identify conventional
(non-neural) torque control.

Between approximately 3.9 and 2.5 seconds before Report:

| Quantity | Recorded range |
| --- | --- |
| Speed | 17.11–17.70 m/s |
| Steering angle | −16.8 to −9.9 degrees |
| Model action, expressed as lateral acceleration | 1.15–1.38 m/s² |
| Measured lateral acceleration | 1.08–1.80 m/s² |
| Requested normalized steering torque | −0.546 to −0.164 |
| Friction contribution, acceleration space | −0.289 to +0.289 m/s² |

The wheel overshoots and unwinds repeatedly while the model's desired bend
changes more gradually. Lateral remains active; steeringPressed is false;
handback scale is 1.0. The EPS driver bound is **not binding** and the bump
scale stays 1.0 in this interval. This is not the removed zero-assistance latch,
a blinker-pause transition, or evidence of a process communication outage.
The seven-second surrounding rlog window contains 700 valid carState messages
with no steeringPressed samples. The telemetry capture has a gap just after
Report, so it must not be treated as an uninterrupted full-rate recorder.

## What the previous unwind fix missed

3.7.11 correctly stopped tracking-error damping from amplifying or reversing
opposite-sign friction compensation. That sign invariant remains necessary,
but it did not address a second case: **jerk-preview friction can request
continued wheel motion while the delayed tracking error points the other way**.
The previous code earned motion credit solely from tracking error, then tried
to transfer it to friction. When those signs differ, friction received no
credit even if signed wheel travel was already closing the friction request
faster than the stored curve reference required.

For example, around −2.504 seconds, tracking error is +0.017 m/s², while the
complete friction input is −0.143 m/s². Signed measured acceleration rate is
approximately −3.20 m/s³, and the stored reference travel over 0.12 seconds is
−0.099 m/s². The wheel is already unwinding faster than that reference, yet
friction still contributes −0.207 m/s². The new independent credit reduces
that contribution to approximately −0.117 m/s² on these same inputs.

This is a confirmed missing damping case in the recorded software. It is not
proof that it is the sole cause of all perceived chop. The physical response
to the changed commands cannot be inferred by replaying unchanged measurements.
The report remains **triaged**, with the source fix linked for follow-up,
until driving validation establishes the effect on the reported symptom.

## Controller change and limits

The existing bounded surplus-motion calculation now also evaluates the full
friction input in its own direction. This includes the existing jerk preview;
it does not change the model's path, curvature interpolation, feedforward
curve demand, or timing. Existing same-direction credit is retained, so the
new calculation cannot restore friction previously removed by 3.7.11.

Both corrections retain the existing 0.12 m/s² credit bound and at least 35%
of the unmodified correction. Credit cannot reverse or amplify friction.
Stationary wheels, movement away from the correction, movement merely matching
the planned reference, low speeds, driver guidance and existing inhibition
conditions do not earn this extra credit. Integration also freezes when the
friction channel alone is earning credit, preventing integral accumulation
from defeating that damping. The neural controller's separate friction path
is unchanged. PID update count, EPS/CarController/Panda limits, physical driver
override, reset behavior and handback scheduling remain intact.

A sampled-input replay uses recorded live vehicle parameters, speed, signed
wheel rate and reference travel. The original friction reconstruction matches
recorded values to floating-point precision. Across 5,914 distinct controller
taps, friction changes in 985; 595 changed frames expose the opposite-sign
blind spot. In the −5 to −1 second corner window, 71 of 323 taps change,
including 45 blind-spot frames. Maximum normalized torque-contribution change
before downstream limits is 0.0414 across the clip and 0.0341 near the report.
No replayed friction magnitude increases or changes sign. This is component
replay, not a closed-loop simulation, exact bus replay or prediction of how much
vehicle oscillation will improve.

## Post-blinker timing

Historical pre-model-gate code at 3.7.8
`d188a6a60cf3096839357f038ebe05a07ec4217c` used `UNWIND_SETTLE_TIME = 0.67`:
**0.67 continuous seconds with the wheel inside 20 degrees of center**.
It had no unconditional maximum. This release intentionally reuses the numeric
duration as an absolute signal-off comfort timeout, as the owner requested;
it does not claim an absolute timeout existed previously.

- Fresh, consistent curved predictions can release the pause after **0.30 s**
  from signal-off, with at least six distinct model frames and the existing
  model/vehicle agreement, acceleration and jerk checks.
- Otherwise, **0.67 s** after signal-off releases the comfort pause. Model
  disagreement, steering pressure, steering angle and speed do not restart
  this timer or impose a near-straight/near-stopped requirement.
- A longer configured reengagement delay remains a minimum for both paths.
- A renewed signal or hazards restart the signal-off timer. Accelerating above
  the trigger speed cannot release a still-signaled turn.
- Missing/stale/nonfinite/malformed model data, invalid vehicle-parameter data
  and an active model lane-change maneuver still prevent release. Once valid
  data returns, an already-expired timer can release without a new dwell.
  Clock rollback/nonfinite clock data cannot manufacture elapsed time.

This clears only the extra blinker comfort pause; MADS/stock engagement,
calibration, service-health, vehicle/steering-fault and standstill checks still
decide whether lateral may actually be active. The existing **15% to 100%
three-second post-blinker torque ramp** is preserved, so timeout release does
not jump directly to full requested torque.

## Diagnostics and verification

The existing control tap adds friction-specific credit, signed measured
acceleration rate, elapsed signal-off time and the last release reason
(`model` or `timeout`). No new IPC subscriptions, schema fields, Params keys
or durable IO in control loops are introduced. Recordings remain private.

**302 host regression tests pass**, including real-controller execution with
isolated IO/model inference, both steering directions, neural/non-neural
transitions, physical driver override, friction sign/bounds, integral handling,
post-blinker torque ramp, curved early release, timeout expiry, configured
delay, renewed signals, malformed/stale data and clock failures. Existing
communication-reader, cruise, download and diagnostic checks also pass.
Ruff and diff checks pass. Two pytest warnings concern optional C++ plugin
configuration, not failing tests.

The new controller cases run against the isolated 3.7.11 controller source
fail for both directions where wheel motion closes opposite-sign friction;
the stationary/away-motion controls pass. The timeout regression also fails
against the 3.7.11 blinker caller. Current source passes those cases.
These host checks do not exercise native device startup or validate physical
steering comfort. The archived private investigation contains replay scripts
and results for the next feedback comparison.
