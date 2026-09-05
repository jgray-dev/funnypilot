# Calibration/engagement audit and 3.7.2 repairs

## Scope and evidence

Compared 3.7.1 `44f14a6af` with fetched remote 3.7.1a `a949212af`.
Repairs are on `funnypilot-3.7.2`, based on its descendant 3.7.1b `950736eeb`
to retain the existing camera-alignment and visible-refusal fixes.

No owner reports were returned by the private feedback inbox query. A parked
SSH attempt to the documented home address returned `No route to host`.
The running device hash, calibration values and actual refusal event therefore
remain unverified. Nothing was flashed, restarted, or recalibrated.

## Findings

### 1. Optional recorder failure became a driving blocker in 3.7.1a

The new `funnypilot_feedback` process is `always_run`. Selfdrived treats a
manager process with `shouldBeRunning` but not `running` as `processNotRunning`
unless explicitly ignored. That event prevents entry and can soft-disable a
running drive. The recorder's startup and finalization paths performed
unguarded filesystem writes. Injecting ENOSPC during triage/finalization
reproduced an escaping exception.

3.7.2 excludes only this optional recorder from that process-health verdict.
Manager still supervises and logs it. Calibration, model, controls, camera,
panda, radar and the unrelated upstream `feedbackd` remain required.
Camera-health checks still run when only the optional recorder is down.

Recorder IO now retries at five-second intervals without replacing its active
Capture object. One accepted failed command is retained with its original
acceptance time, preventing freshness expiration from cancelling its IO retry.
New stale requests remain invalid. Failed error-status writes cannot themselves
kill the daemon. Unknown programming errors are not silently swallowed.

Capture metadata and labels advance only after persistence. Initial pre-roll
and final triage use temporary artifacts; retries preserve already collected
snapshots when metadata or directory fsync fails. Interrupted telemetry is
explicitly marked. Saved-route protection and bounded corner relief are unchanged.

This is a reproduced regression, **not proof the owner's recorder crashed**.

### 2. Camera mismatch is real but predates 3.7.1a

Camera normalization and model projection used the inner viewport, while video
was rendered into the outer rectangle. On the 2160x1080 layout this produced
2160/2100 horizontal and 1080/1020 vertical scale disagreement. Calibration
angles cannot repair two rendering scales. The inherited 3.7.1b fix uses the
inner viewport for both and includes its x/y position in the matrix cache.

The latest audited 3.7.1a C3X overlay path already requires valid, fresh,
current-drive calibration with `calStatus == calibrated`. If that exact code
is running and those are its projected overlays, visible lanes are not evidence
that calibration never completed. Check the running hash and live values,
rather than resetting calibration based on the picture alone.

3.7.1b's inherited availability messages expose calibration and engagement
blockers even when the state machine has not selected a no-entry popup. This
changes visibility, not calibration generation or required engagement checks.

### 3. Legacy zero-delay probe is separate

Two prior first-update probes failed with the explicitly selected legacy v0
torque controller, active, and zero `liveDelay.lateralDelay`. Its jerk formula
divides by the unclamped delay. This predates 3.7.1a, is not reached by inactive
updates, and does not establish the reported initialization failure. No controller
retuning or unrelated zero-delay behavior change is included here.

### 4. Verify itself had false failures

Source markers containing brackets or `*` were interpreted as grep regexes;
a label containing an apostrophe also broke shell quoting. The shipped command
now uses literal grep and shell-quoted arguments. A test executes the actual
command template against all 172 markers rather than checking Python substring
membership alone.

## Validation

- Final documented import-light regression suite: **1,405 passed**. Command is
  in `docs/controls-review-3.7.1a.md`; used `/tmp/fp-audit-venv/bin/python` and
  its documented native-only exclusions.
- Ruff clean across `selfdrive/ sunnypilot/ system/ common/`.
- Camera/alert/feedback focused suite: 87 passed before the extra diagnostic
  command regression test was added; all are included in the final suite.
- Mutation checks in disposable temporary trees: restoring optional-recorder
  gating caused two test failures; restoring the outer video viewport caused
  all four narrow/wide/sidebar geometry cases to fail. The working tree was
  not mutated by these checks.
- Fault injection covers startup, socket cleanup, finalization, initial reports,
  rule/label persistence, status writes, actual directory-fsync failure after
  rename, partial samples, long-outage retry, and the production main-loop wiring.

These checks do not run the ARM device, its live cameras or vehicle plant.
Actual recovery still requires comparing device identity, calibration
status/angles, blocking events and process health together when parked access
is available. No engagement bypass for bad calibration is included.
