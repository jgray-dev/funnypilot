# 3.7.11: steering-wander regression, unwind and active-drive downloads

Based on clean 3.7.10 `71d40983c946861a570c6adedb7e5506544c23ef`.
Published source only; no device install, restart or vehicle validation in this
session. The 3.7.8 communication/minimap fixes and 3.7.10 cruise changes remain.

## Confirmed steering-wander regression

Both new steering-wander reports record clean 3.7.10 at the exact base commit:

| Report | Zero-latch window relative to Report | Recorded behavior |
| --- | --- | --- |
| `737f558f91ab4eb1b6521a046d4b40d8` | −9.048 to −3.788 s | Lateral stays active; assistance scale/request are zero through a change of bend direction. |
| `a32fd44101ee4df5881144eb182902df` | −7.483 to −4.752 s | Small opposing request followed by steering demand agreeing with the driver; assistance remains zero. |

Reviewed labels, manifests, road-video samples, telemetry/controller samples and
full log segments together. CarParams confirms KIA_K5_2021 torque control; taps
confirm the conventional, non-neural controller. Video shows daytime roads;
the second clip includes a lead vehicle. Neither labels nor video alone prove
controller causation: the decisive evidence is the active state, nonzero model
turn demand and explicit handback latch/zero request.

The defect was introduced in 3.7.10. A driver-force sample at the physical
opposing-zero threshold (242 K5 counts) latched **all** requested torque to zero.
It stayed latched while steeringPressed remained true, even after the model's
request reversed and assistance would agree with the driver's correction.
Release required a quiet interval followed by a 2.5-second ramp. A renewed
press could pause that ramp near zero, or latch its low scale again. This was
extra software suppression, separate from the directional physical limits.

The finite-force zero latch is removed. Comfort still grades assistance from
85% toward 60%; it does not classify driver intent or disable both directions.
The unchanged EPS governor remains last, enforcing directional driver limits,
slew and damped bound recovery; CarController and Panda remain independent
backstops. Large opposing force can still physically remove opposing torque.
Assistance agreeing with the driver's new direction can return without a
hands-off interval. Nonfinite handback inputs still latch a zero target until
valid quiet release, with integral freeze. This is input-fault handling, not
an attempted interpretation of driver intent.

Sampled-input replay gives nonzero comfort scales throughout both previously
latched windows (approximately 0.60–0.81 and 0.61–0.91). This replay is limited
by telemetry sampling and does not predict the changed vehicle motion. The
production-controller regression separately verifies both directions and both
controller modes: opposing force still suppresses output through EPS limits,
but assistance can follow a reversed demand while driver pressure remains.

## Choppy unwind: friction correction could be amplified or reversed

The controller previously reduced tracking error by motion credit, then added
jerk preview to construct friction compensation. At corner exit, those two
terms may oppose each other. Subtracting credit from the tracking term could
**increase the opposite friction request**, or push a small combined request
through zero. This is a sign interaction, not a reason to delay model knots.

The two wander clips contain 72 and 33 distinct frames with opposite-sign
friction amplification; 43 and 11 of those have a decreasing-magnitude curve
reference. They also contain 8 and 2 friction-input sign reversals. The newer
`2c0324730fd344e49ac6986e76a02633` slow-response clip contains the same defect.
Reconstruction of the original friction contribution matches the recorded
values exactly using the recorded friction coefficient/factor and zero deadzone.

The fix constructs the undamped friction input first and transfers existing
motion credit only if that input agrees with the tracking error. Transfer
cannot increase magnitude, reverse sign, reduce below the existing 35% floor,
or exceed the existing 0.12 m/s² credit budget. Base proportional-error damping,
model curvature/timing, physical limits and the single PID update are preserved.
The recorded affected windows use the conventional controller; this change does
not retune the neural model's separate friction path.

Counterfactual friction replay changes 80, 42 and 30 frames in the three clips;
maximum normalized torque-contribution differences are 0.0289, 0.0194 and
0.0141 before downstream limits. This verifies removal of an actual amplification
mechanism. It does not prove every perceived unwind jolt has the same cause or
that the physical unwind now feels smooth; that remains vehicle validation.

## Reengagement after signals while guiding a curve

The model gate already accepts consistent curved predictions, but its caller
reset the entire dwell whenever steeringPressed was true. That imposed a
hands-off condition even when the model agreed with the manually guided curve.

The clean 3.7.10 slow-response report `2c0324730fd344e49ac6986e76a02633` contains
such a pause. A replay using recorded model, vehicle parameters and car state
finds eight paused model frames that satisfy the original stability checks
while steeringPressed is true. One is approximately −6.99 seconds from Report:
speed 14.17 m/s, desired lateral acceleration 0.455 m/s², measured 0.204 m/s²,
and driver sensor torque −167 counts. That is moving, gently curved handover.
Consumer timing is reconstructed from log timestamps; this is not exact device
SubMaster scheduling or proof of the eventual release time.

Only the pressure-only veto is removed. Signals/hazards still hold a triggered
pause. Fresh distinct model frames, the 0.67-second minimum or longer configured
delay, model maneuver exclusion, acceleration/jerk limits and measured-curvature
agreement still decide release. The existing gradual reengagement torque ramp
and physical override remain. A model that disagrees with the driver's curve
cannot unlock merely because it is temporally consistent.

## Downloads while the vehicle is on

Video, data and combined archive requests no longer check IsOnroad or return
the parked-only 503. The existing dashboard buttons remain available. Exports
run through the single background worker, which drops inherited real-time
scheduling/control-core affinity and uses lower priority. Each socket write
awaits backpressure; memory stays bounded by 256 KiB file chunks, without
compression or whole-drive buffering.

Allowing live recordings exposed another defect: tar headers declared a size
before reading, but the old loop read until EOF. If loggerd appended data, the
extra bytes would overrun that header and corrupt subsequent members. Exports
now open each file and snapshot its size, then read exactly that byte count.
Unlink after open does not lose its bytes; missing files before open are skipped.
Truncation/read failure aborts the connection visibly; cancellation closes the
iterator/file on the same worker. Tests cover growing files, truncation, unlink,
on-road requests, selectors, background execution, disconnect and cleanup.

An active file is a snapshot of bytes written so far, not a promise that its
video/compressed-log codec has been finalized. New segments created after the
initial listing are not included; retention may remove a not-yet-open file.
Exports do not change explicit saved-drive flags. Timeline decompression retains
its separate on-road budget. Automated feedback uploads remain parked/Wi-Fi.

## Verification

The combined host regression suite passes **287 tests**. Ruff and diff checks
pass; two pytest warnings concern optional C++ plugin settings.

Production-path and invariant tests cover driver guidance/direction
changes, physical override, neural/non-neural transitions, integral handling,
friction-credit bounds, curved model handover, active downloads and fault
injection. Native device
startup and physical steering validation were not performed. No new IPC readers,
compiled schema fields, Params keys or durable control-loop IO were introduced.
