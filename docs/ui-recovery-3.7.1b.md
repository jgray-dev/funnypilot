# 3.7.1b: visible blockers and camera alignment

The owner still reported misaligned lanes/path and no engagement or error popup
after 3.7.1a. SSH was unavailable while driving; the private feedback inbox had
no owner reports. The following are reproduced code defects, not a confirmed
diagnosis of the device's current refusal.

## Camera projection

The C3X road view calculated video normalization and model projection using
the inner viewport (30 px border on each side), then drew camera pixels into
the outer rectangle. At 2160 x 1080 this scaled camera pixels by 2160/2100
horizontally and 1080/1020 vertically relative to the projected geometry.
Calibration cannot correct two different rendering scales.

Video now uses the inner viewport too, matching the existing small-screen
renderer. The matrix cache also includes the viewport's x/y origin so moving
a viewport without resizing it cannot retain an old projection.

## Alerts without an engage request

The stock state machine only selects many noEntry alerts after recognizing an
enable event. During initialization it does not advance that state machine;
an unrecognized button event can also leave the selected alert empty. Rendering
every selected fault therefore did not guarantee a visible explanation.

After a five-second startup grace, the C3X renderer now reads existing messages
to explain calibration progress/failure, unavailable streams, and stock/MADS
blocking events when there is no selected alert. Process-not-running events
include stopped process names when fresh manager data is available. Existing
selected faults and takeovers retain priority. The exact routine notice list
is unchanged; a noEntry alert is never a quiet notice. No controller state,
calibration data, or safety condition is modified by this display fallback.

Faults render after camera scissor teardown and all HUD/border drawing. The
Report popup yields to the actual rendered alert, including these fallbacks.
Feedback captures retain both blocking event lists and their validity.

## Validation

The existing regression command in `controls-review-3.7.1a.md` passes 1,358
checks on this branch. Focused tests use real capnp messages and the production
alert selector, covering empty selected alerts, initialization, MADS events,
invalid services, process names, recovery and takeover priority. Four geometry
cases run the production camera-draw and projection methods, for both cameras
and two sidebar layouts, comparing the same image points numerically.

A local Xvfb/Mesa check ran the production mid-alert drawing methods with real
raylib and repository fonts. Calibration and process-blocked messages were
visibly rendered. Graphics surroundings and incoming messages were fixtures;
this does not validate the device's ARM runtime, camera feed or vehicle behavior.
Ruff and diagnostic code markers pass. No device flash/restart was performed.

When parked access or a report becomes available, verify the actual running
version/commit, calibration status/angles, selected alert, both event lists and
process health together. Do not reset calibration or bypass an engagement
constraint merely because the popup was missing.
