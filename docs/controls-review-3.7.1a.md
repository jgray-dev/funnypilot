# Controller review: FunnyPilot 3.7.1a

Integrated base: `funnypilot/funnypilot-3.7.1`, commit `44f14a6af`.
Initial work started from 3.7.0 (`73a974640`); the complete 3.7.1 release was
subsequently merged into the published branch after the owner corrected the base.
Working branch: `funnypilot-3.7.1a`. Review date: 2026-09-05.

This change fixes reproducible controller defects. It does not establish
autonomous-driving capability or road readiness. No device was flashed.

## Lateral follow-up: steering motion credit

The owner's next request concerned occasional large wheel movements during a
turn. The new mechanism acts inside torque error feedback: it asks how much
of the correction is already being supplied by wheel movement, relative to
the upcoming reference trajectory. It does not move the model's curvature
knots or spread them across a longer window.

`SteeringMotionCredit` estimates signed wheel motion from 80 ms of angle
samples. This matters on the K5: its CAN `SAS_Speed` field is unsigned and
quantized in 4-degree/second steps. The estimator subtracts one 0.1-degree
angle quantum, needs 40 ms of history, and clears its direction history on a
reversal or sample gap. It therefore prefers missing a small movement over
inventing motion from a quantized sensor tick.

The controller projects that motion at most 120 ms forward and subtracts the
reference movement already known from the actuator-delay buffer. Only surplus
travel toward the tracking error earns credit. It smoothly reduces the error
used by proportional and error-friction correction, and freezes integral
accumulation while meaningful credit is applied. The maximum credited error
is 0.12 m/s²; at least 35% of proportional/error-friction correction remains.
Static path feedforward, roll/offset compensation, and planned jerk retain
their values. Driver intervention, bump handling, and limiting bypass credit.

Neural control receives the same bounded credit, adapted to its reference
horizon; it cannot turn a small delayed error into a large reduction of a
future error, or soften an opposite-sign future error. Its plan inputs are
unchanged. The v0 torque tune keeps the original behavior.

There is no accumulated command waiting to be paid back. When the wheel
stops moving or falls behind a planned ramp, credit disappears and the
original correction is available again. This is the distinction from a
longer smoothing filter that would delay the entire turn.

In five simple delayed second-order rack probes using the real conventional
controller, wheel-rate RMS decreased approximately 1.3–4.2%; tracking-error
RMS changes ranged from about 5.5% better to 2% worse. Torque-rate RMS improved
in four probes and worsened approximately 0.5% in one. These are modest
mechanism checks on an uncalibrated synthetic rack, not vehicle-performance
claims. The actual driver-reported symptom still requires drive evidence.
Existing triage records now carry `mcs` (minimum base feedback scale) and `mcr`
(maximum credited error) to correlate the change with that symptom.
Five focused checks cover the bounded behavior and real-controller wiring.
Three additional mutations (removing conventional/neural credit and using the
unsigned rate signal) each failed the corresponding behavioral check.

## Control paths reviewed

Lateral: model curvature and delay alignment in `controlsd`, predictive knot
damping, the 20-to-100 Hz interpolator, curvature limits, angle/PID/torque
controllers, both torque versions, neural feedforward, handback, bump damping,
and the EPS governor. The final vehicle interface and Panda still enforce
actuator limits. The fork's EPS constants and locked acceleration factor are
specific to its K5 setup; this review does not establish other-car suitability.

Longitudinal: model parsing and ACC/experimental arbitration, the MPC's lead
and cruise obstacles, headway and lead-deceleration belief, stopping governor,
lead-loss grace, SCC vision/map fusion and learning, speed-limit targets,
turn/coast limits, acceleration shaping, publication, and the
off/starting/PID/stopping state machine.

The highest-value defects were at boundaries between these components.
Changing gains before correcting those boundaries would obscure the cause.

## Implemented findings

| Finding | Previous behavior | Result |
| --- | --- | --- |
| Neural PID ran twice | The conventional acceleration-space update and the neural torque-space update both changed one integrator. Only the second was visible in the log. | Exactly one update per tick. A deterministic fixture's integral increment changed from approximately 0.000165 to 0.000015, the independently calculated neural increment. Both torque versions are covered. |
| PID units survived mode changes | Neural/conventional transitions retained an integrator in the other controller's units, with limits dependent on update order. | `prepare_pid` selects limits every tick and resets state when the unit system changes. |
| Neural handback omitted a freeze | The base controller froze its PID during handback, but NNLC updated it again with a different freeze condition. | The complete caller decision reaches the neural update, including handback, bump, and EPS suppression. |
| Lateral disengagement retained integral state | The inherited reset cleared only the saturation timer. | Conventional PID and both torque controllers reset the integrator. Torque resets also clear neural history. |
| Neural model validation was incomplete | Roll length alone admitted missing/short/nonfinite pitch and acceleration arrays to interpolation and differencing. | All three consumed arrays must match the model time grid and contain finite values. Otherwise conventional torque control runs with its own limits. |
| Engagement phase bypassed smoothing | Engagement between model frames passed the complete cached curvature action straight through. | The cached action starts the same first interpolation segment as an action received on that frame. Existing knot timing and spline shape remain intact. |
| Comfort shaping postponed braking | A -3.5 m/s² demand from +1.0 took eight 50 ms updates to reach its target. Mild braking could still leave positive acceleration commanded initially. | New nonpositive reductions pass through immediately. Throttle application, partial positive lifts, and brake release keep their comfort shaping. |
| Stop-hold ignored planned braking | Entering stopping from zero replaced a -3.0 m/s² demand with -0.004 on the K5's first frame. | Stopping preserves the stronger of planned and existing braking, then uses the unchanged stop-hold rate. |
| Shaper memory differed from published output | Turn/coast clipping could leave hidden positive acceleration in the shaper while the published target was negative. | Every published acceleration reseeds the shaper. Release begins from the command actually sent. |
| Invalid acceleration released braking | NaN or infinity reset the shaper to zero, including during braking. | Invalid targets remove positive acceleration and retain existing negative acceleration. This is transient containment; it is not a complete sensor/solver-failure policy. |
| Stop-governor reset mixed two lifecycles | Keeping evidence to permit arming also kept it across disengagement; invalid range data retained confirmation. | Full reset drops lead evidence. Internal cap reset preserves evidence only while observing a usable track. Invalid ranges require fresh confirmation. |
| Processing delay mixed units | Publication seconds minus model nanoseconds produced a large negative diagnostic value. | Timestamp subtraction occurs in nanoseconds before conversion to seconds; a 50 ms fixture publishes 0.05. |

## Preserved decisions and limits

The existing spline timing, predictive-filter gains, lateral acceleration/jerk
rails, EPS torque bounds, driver override scaling, launch calibration,
following-distance tables, the 0.93 cruise-target multiplier, MPC costs and
generated solver, and SCC/SLA tuning
are retained. The 3.7.0 curve-learning protections for turns, manual passes,
and demonstrations are also retained.

`CLAUDE.md` documents prior regressions from adding steering lag, weakening
the sole stopping controller, maintaining inconsistent consumer paths, and
testing duplicate arithmetic. The implementation avoids new steering filters,
preserves stop-hold rate, passes the freeze decision between controllers, and
tests production methods and publication rather than copied formulas. No
Params keys, capnp schema changes, or generated-code changes are introduced.

## 3.7.1 integration

The complete 3.7.1 release is retained: the minimal white/red guide bar,
proportional corner-cap release from the apex, model-path opening allowance,
cruise offset before governor arbitration, route-matched learned corners,
corner visibility through run-out, and corrected MAP/LRN indicators.
The merged planner keeps the braking shaper synchronization and timestamp fix.
The steering changes are unchanged by this merge. Both releases' diagnostic
markers are retained, with the displayed version remaining 3.7.1a.

## Validation

The unmodified controller/governor baseline passed 724 tests. The expanded
regression run after integrating 3.7.1, drive retention, live feedback and the alert-visibility hotfix passes **1,345 tests**, including lateral/longitudinal helpers,
actual controller transitions, planner output integration, SCC/SLA, blinker
pause, UI import/contract guards, diagnostics, and release metadata checks.
Ruff passes across `selfdrive`, `sunnypilot`, `system`, and `common`.
The drive-retention addition has 12 focused checks against temporary recordings
and real HTTP handlers. A mobile Chromium check also covers save, reload,
saved filtering, protected deletion, playback unsave, and confirmed deletion.
Its local server uses temporary fixture files, not device recordings.

Live feedback adds 12 focused capture, multi-label popup, real message-schema
and SCC consistency checks. Invalid cruise/context values do not earn relief.
The private Worker passed local authentication, incomplete-upload, checksum,
retry and manifest-conflict checks. A synthetic capture uploaded to production
and downloaded through the agent CLI with part and whole-file SHA-256 checks.
That report is labeled synthetic and excluded from the owner feedback inbox.
It verifies the pipeline, not steering or braking behavior. The Worker passes
TypeScript checking and Wrangler deployment validation. Native UI rendering,
the complete daemon lifecycle and onroad behavior remain unverified here.
Desktop and mobile Chromium checks exercise the real Feedback HTTP handler,
multi-label/status rendering, private config filtering and HTML escaping.
See `docs/feedback-workflow.md` for capture, bounded relief and review details.

The visibility hotfix adds 16 checks covering normal-severity refusals and
calibration faults, unknown alerts, quiet routine notices, process watchdogs,
and geometry withdrawal/recovery with invalid or stale calibration/model data.
The capture schema check also verifies calibration and refusal fields. The
owner's device was being driven away from SSH and the private inbox was empty;
these checks do not identify or resolve its actual engagement blocker.

Thirteen mutations were applied individually, confirmed to fail their targeted
behavioral test, and restored: double PID integration; omitted handback freeze;
omitted torque reset; retained PID units; acceptance of incomplete neural
plans; braking comfort delay; unsynchronized clipped shaper state; discarded
stopping demand; retained inactive lead evidence; invalid-target brake release;
invalid-range acceptance; mixed timestamp units; and missing first steering
segment.

The steering suite uses real capnp messages, PID, controllers, neural input
construction, handback, and EPS governor. Params IO and vehicle responses are
fixtures. An analytical inference response permits an independent integral
oracle; a separate test runs the actual `KIA_K5_2021.json` neural model.
The planner suite runs the real update and publish methods against prescribed
solver trajectories and isolated speed-governor/IPC boundaries. **Neither
suite is a closed-loop vehicle simulation.**

Reproduce the regression run from the repository root with Python 3.11 and
pytest, numpy, pycapnp, setproctitle, zstandard, aiohttp, requests, pyzmq,
parameterized, pytest-asyncio, pytest-cpp, and pytest-mock installed:

```bash
PYTHONPATH=. python -m pytest --noconftest -q -p no:cacheprovider -o addopts='' \
  selfdrive/controls/lib/tests \
  selfdrive/controls/tests/test_longcontrol.py \
  selfdrive/controls/tests/test_drive_helpers_smoothing.py \
  sunnypilot/selfdrive/controls/lib/long_v2/tests \
  sunnypilot/selfdrive/controls/lib/speed_limit/tests \
  selfdrive/ui/sunnypilot/onroad/tests sunnypilot/navd/tests sunnypilot/tests \
  sunnypilot/feedback/tests \
  sunnypilot/selfdrive/car/tests \
  system/manager/tests/test_storage_cleanup.py \
  sunnypilot/selfdrive/controls/lib/tests/test_blinker_pause_lateral.py \
  --ignore=sunnypilot/selfdrive/controls/lib/speed_limit/tests/test_speed_limit_resolver.py \
  --ignore=sunnypilot/selfdrive/car/tests/test_cruise_mode.py \
  --ignore=sunnypilot/selfdrive/car/tests/test_custom_cruise.py
ruff check selfdrive/ sunnypilot/ system/ common/
```

The existing following-distance simulation could not collect here: the
checkout contains ARM aarch64 `msgq`, Params, and acados extensions on an
x86_64 host. This is not recorded as a passed or skipped vehicle test.

## Required before driving

Run the native build, following-distance/lead maneuvers, controller tests, and
process replay on compatible runtime artifacts. Compare 3.7.1 and 3.7.1a on
the same recorded inputs before device deployment. Closed-course acceptance
must cover lead braking and cut-ins, low-speed stopping/launch, changing coast
limits, steering disengagement in a curve, driver handback, neural model
dropout/recovery, and steering saturation.

New braking reductions can arrive more abruptly because a downstream comfort
delay has been removed. Neural integral response is weaker where the old
double update supplied unintended correction. These are intentional command
changes, but their effects on ride comfort, tracking error, jerk, stopping
distance, and driver intervention rate require vehicle evidence. Passing the
tests above is not a substitute for that evidence.
