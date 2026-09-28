# 3.7.10: curved handover, driver corrections and cruise resume

Based on clean 3.7.9 `7eed85d6b3bde13f01946b5e0e1a4b9cc742603b`.
Source release on `funnypilot-3.7.10`; no device installation or restart in this
task. Physical steering feel and vehicle button behavior still need validation.

## Model stability after a signalled turn

3.7.9 already permitted a constant-radius curve; it did not literally require
straight steering. It nevertheless compared fixed prediction horizons across
frames and required the entire next second's acceleration range to fit within
0.5 m/s². A consistent model following a gently changing curve could repeatedly
restart the dwell because the prediction advanced along that curve.

3.7.10 compares overlapping predictions at matching future instants, using the
existing consumer receive clock. The action's offset from the near-term path
must also remain consistent. An unpredicted drift still disagrees with older
overlapping plans, rather than passing a succession of tiny adjacent changes.
The next second may evolve at up to 1.0 m/s³ across its half-second samples.

The 0.67-second minimum (or longer configured delay), ten distinct frames,
150 ms freshness/gap limit, lane-change exclusion, 2 m/s² acceleration bound,
and 0.5 m/s² action-versus-measured handover bound remain. Action must also
agree with the near-term plan within 0.5 m/s². Driver press/signals still pause
the dwell. The existing post-blinker torque ramp and actuator limits remain.

A synthetic coherent curve with 0.7 m/s³ evolution does not unlock in 3.7.9's
first 0.7 seconds; the new gate unlocks at 0.7 seconds, on the first model frame
after the minimum. Both left and right curves are covered. This is a logic
regression check, not a prediction of the vehicle's subsequent path.

## Driver corrections and larger interventions

A steeringPressed event does **not** itself disable lateral control in this
path: the overriding state is still active. There are independent mechanisms
which can reduce torque: the comfort handback scale, integrator bleed, and the
physical driver-torque bounds mirrored by the EPS governor. Actual faults,
explicit disengagement and blinker pauses can still disable lateral control.

The old comfort scale approached 60% after any sustained press, regardless of
how small the tracking disagreement was. Its return became faster with greater
disagreement: from 1.6 seconds down to 0.45 seconds. Those are reproducible code
behaviors, not proof that either explains every reported steering oscillation.

The revised schedule:

- Small disagreement (up to 0.4 m/s²) and moderate driver force retain up to 85%
  of the unscaled request. Larger disagreement (through 2.5 m/s²) or force
  (150–242 K5 sensor counts) grades this down toward the existing 60% scale.
  This remains total-request scaling; it does not estimate a perfect amount of
  corner-holding feedforward or learn a permanent lane-position preference.
- Full opposing force uses the existing physical zero-authority threshold,
  derived from the EPS mirror: `(384/2 + 50) / 1 = 242` counts. If it opposes
  the previous actuator request, the target becomes zero without the comfort
  dwell. Yield stays latched until the release hysteresis completes. A strong
  force aiding the current request does not trigger this full-yield latch.
- The EPS governor remains last. Its torque bounds, damped recovery and slew,
  CarController and Panda checks remain unchanged. Zero target is not a promise
  of instant zero physical torque; the actuator slew still applies.
- Assistance cannot increase while the sustained press remains latched.
  Return takes 1.6–2.5 seconds; more disagreement or full yield earns the longer
  return. Any new press pauses restoration immediately, even before the comfort
  dwell. Resuming the ramp starts smoothly from the current scale.
- Existing integral freeze/bleed, single PID update per tick, mode resets and
  motion-credit exclusions remain. Nonfinite handback inputs yield a zero
  target and keep the integrator frozen.

Force and disagreement are **heuristics**, not reliable knowledge of intent.
The hardware may still remove assistance during a forceful correction. The
change cannot guarantee prevention of flicking, evasion recognition or safe
on-road performance without vehicle validation. It avoids toggling latActive
for ordinary steering pressure and records handback target, yield latch and
return state through the existing feedback tap, with no new IPC subscriber.

## Fresh SET and physical RESUME

Fresh non-PCM SET captures the existing initial speed plus exactly 2 mph
(3.218688 km/h), clipped to the existing maximum. RES restores the saved target;
it does not repeatedly add the offset. This follows the owner's clarification.
PCM-owned targets and the planner's lead, stop, acceleration and SLA constraints
remain under their existing owners.

The K5's physical RES_ACCEL is already mapped to accelCruise, whose release
creates buttonEnable. No new engagement bypass is needed. A separate defect
exists in speed initialization: card used only CS_prev's one-frame button event
after enabled returned through selfdrived/controlsd. A delayed response could
lose RES intent and capture the current, slower speed instead of the saved one.

The helper now retains disabled-state SET/RES release intent for at most 100
card frames (one second nominally). Cancel, main-cruise changes, unavailable
cruise, expiry and consumption clear it. It only selects a target **after** the
ordinary enable response; it cannot enable the vehicle. Brake/fault/no-entry
checks and first-RES-without-an-initialized-target refusal remain unchanged.
Gas override remains under the normal pedal behavior; RES cannot override a
held brake or force engagement while the cruise system is unavailable.

A production-helper regression with a three-frame delayed response previously
restored 43 km/h instead of the saved 72 km/h. The new implementation restores
75.218688 km/h, exactly its previously saved, offset-adjusted target. Repeating
RES does not increase it. The test covers both resume enum names, metric and
imperial display modes, SET after braking, expiry/cancel, and PCM ownership.

## Feedback evidence

New report `741e059967634c03a0d8e5d98428e762` is unlabeled and records clean 3.7.9
`7eed85d6b`. Its telemetry covers monotonic 37.019–102.465 seconds; the recorded
wall clock is inconsistent with the investigation date and is not used to
assign a drive date. Reviewed telemetry, road-video samples, controller samples
and both full log segments together. The video shows a nighttime wet road.
CarParams confirms KIA_K5_2021, non-PCM cruise and openpilot longitudinal.

No steeringPressed samples coincide with lateral-active telemetry; override
scale stays 1.0. SET events occur, but no RES event occurs in these segments.
The later pause contains signal, driver-press and low-speed exclusions. A
counterfactual gate replay over 75.8–102.46 seconds gives 16 passing model
frames for both versions using reconstructed event timing. It does not
establish a faster handover in this clip or validate the new nudge behavior.
The report is recorded as **triaged**, not fixed. Older bite reports remain
triaged as described in `release-3.7.9.md`.

## Verification

The focused host regression suite passes 160 tests. It exercises the real
non-neural/neural torque controllers in both directions, driver yield and
return, integral handling, unchanged EPS/Panda-equivalent bounds, model gate
faults, cruise initialization, SLA behavior, feedback tap, follow-distance
handling, reader census, imports and diagnostic markers. Ruff and diff checks
pass. The three pytest warnings concern optional host plugin configuration.

Native Params/IPC and physical CAN/EPS were not run on the vehicle. Production
controller tests isolate Params and neural inference inputs; the physical RES
mapping test executes source AST to avoid loading native CANParser. These are
host tests, not native startup or road validation. No schema, Params key,
compiled module, new subscriber, uploaded recording or credential is added.
