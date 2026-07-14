FunnyPilot v3.3.3st (2026-07-14)
========================
Stable snapshot of 3.3.3 plus three hands-off quality-of-life features:
a hidden cruise-only speed governor, unattended model/map refresh while
parked on WiFi, and a live-learned steer-delay readout on the dev UI.

* feat(hidden cruise governor): restores the pre-3.2.6e "hidden speed
  offset" concept (previously 0.9x, removed in the single-authority
  longitudinal rewrite for predictability). `HIDDEN_CRUISE_OFFSET = 0.93`
  in `longitudinal_planner.py` shaves 7% off the cruise-only target speed
  before it ever reaches the MPC/controls layer — nothing in the UI or
  car state shows it. It only applies while simply tracking the set
  speed: gated off the instant a lead is being followed (lead0/lead1) or
  a forced decel is in progress, so lead braking and safety stops are
  completely unaffected.
* feat(auto-updater): new `sunnypilot/auto_updater/manager.py` daemon
  (`only_offroad`-gated, mirrors `models_manager`/`mapd_manager`). Tracks
  continuous WiFi (`deviceState.networkType == wifi`) while parked; once
  held for 15 minutes it re-triggers the same actions the Settings
  "CHECK"/"Database Update" buttons do — nudges `ModelManager_DownloadIndex`
  to the currently active bundle (hash-verified per-file, so it's a
  no-op unless the bundle's remote content changed) and, if a map region
  is configured (`OsmLocal`), sets `OsmDbUpdatesCheck` to refresh the OSM
  data. Re-arms every 15 minutes so a long parked/charging session keeps
  refreshing both.
* feat(dev UI lagd readout): bottom developer-UI bar gains a "LAGD" tile
  (`LagdElement` in `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py`)
  showing `liveDelay.lateralDelay` live (green when the live-learner's
  estimate is vetted/`estimated`, red if `invalid`, white while still
  `unestimated`) — the same value the Models page's "Live Learning Steer
  Delay" toggle feeds from, now visible on the road without opening
  Settings.

FunnyPilot v3.3.3 (2026-07-13)
========================
SLA gets its original arrow activation back, learns to gas-gate BEFORE a
lower speed limit zone, and the logging/web UI is decluttered. Lateral is
untouched (3.3.2 feel carries over byte-identical).

* feat(SLA activation, the original controls + UI): when long control is
  engaged and a speed limit is known — on engage or on entering a new zone
  while SLA is off — the sign pulses and shows an up/down arrow for 6
  seconds: up if your set speed is below the limit, down if above.
  Pressing the cruise button IN THE ARROW'S DIRECTION during the window
  activates SLA; the press is swallowed and your CURRENT SET SPEED IS
  ADOPTED UNCHANGED — no jump, no jerk. The %-offset is derived from where
  you already are (confirm at 50 set in a 45 zone = active at +11%, still
  doing 50). If the set speed already equals the limit, SLA activates by
  itself at 0%. The window simply times out otherwise and re-offers at the
  next zone. Everything AFTER activation is the new stack, unchanged: the
  cluster set speed is the target, manual adjustments re-derive the
  carried %-offset (60 in a 50 -> +20% -> 36 in a 30 zone), zone snaps are
  idempotent, deactivation only on disengage/mode off.
* feat(SLA pre-zone gas gating): approaching a LOWER zone while active,
  once inside the coast envelope ((v^2 - v_target^2) / (2 * 0.35) plus a
  1.5 s early-arrival buffer, target including your %-offset), the planner
  clamps max accel to the measured coast accel — the same mechanism the
  model's allow_throttle uses. No throttle, NO brakes: the braking floor
  is untouched (lead braking unaffected), and the cruise target does not
  drop until the boundary, so nothing can command brakes for the new zone
  early. Enter the zone near target; any residual overspeed is shed by
  the MPC as normal LIGHT braking after the boundary.
* fix(resolver): the upstream "adapt to the upcoming limit early" switch
  (marked FIXME/not-working upstream) is removed — it flipped the resolved
  limit ~80 m early, which would have made the SLA set-speed snap fire
  before the zone, i.e. braking before the sign. The resolver now changes
  exactly at the boundary and instead exposes the upcoming limit +
  distance (speedLimitAhead) continuously for the gas gate.
* chore(logging cleanup): web UI Verify consolidated from ~31 rows to 7 —
  version / branch / working-tree-clean / ONE "shipped code markers" check
  (16 load-bearing greps, fails naming whatever is missing) / updater
  target+staged / active model bundle / triage log sizes. The inline
  log-tail rows are gone (that's what the Logs viewer is for).
  lat_interp.jsonl no longer writes a record every parked second: idle
  time collapses to one {"idle": N} heartbeat per minute, and the first
  driving record carries the skipped count. radar_tracks.jsonl likewise
  collapses zero-track seconds to a 30 s heartbeat — "n stuck at 0 while
  driving" is still fully visible, without 86k identical lines a day.
* tests: SLA suite rewritten for the arrow flow (30 cases: directional
  confirm both ways adopts the set speed, wrong-direction ignored, stale
  press expiry, window timeout + re-prompt, adopted ratio carries to the
  next zone, ratio carryover incl. both user examples, gas gate envelope
  on/off/ratio-aware/clears-at-boundary); triage idle-collapse cases
  added (112 total import-light tests green).

FunnyPilot v3.3.2 (2026-07-11)
========================
REVERTS the v3.2.12 adaptive EMA smoothing after on-road falsification —
steering was starting EARLIER than the validated feel ("a 2.5 in the space
before we'd usually see the 5"), and raising the delay knob only smeared it
further into sloppiness.

* post-mortem (recorded so it is never retried): the v3.2.12 idea was
  "EMA the knots with tau, sample the plan tau earlier, lag is pre-paid, so
  the effective total is unchanged". The flaw: an EMA does not DELAY a
  maneuver, it REDISTRIBUTES it. For a turn onset, the filtered command
  begins moving immediately at the (earlier) sample point and creeps
  through partial values across the window — so the wheel is visibly
  turning where the pre-3.2.12 command was still flat before its decisive
  step. "Total-preserving" holds for steady-state phase, not for onset
  shape, and onset shape is what hands feel. No delay-knob setting fixes
  it: more delay = an earlier sample point = more smear.
* the delay window IS still used for interpolation — the way that was
  always validated: the models-page delay makes every 20 Hz knot a
  delay-compensated preview, and controlsd's LatSmoother spreads each knot
  delta across the control frames (delta/5). That stays, untouched.
* reverted: modeld_v2 adaptive tau + horizon shift (bundle 'lat' override
  restored to plain upstream additive semantics; network delay input
  unchanged throughout); stock modeld same; smooth_seconds_for_delay
  removed from lat_smooth.py (a DO-NOT-REINTRODUCE note remains).
  controlsd's setpoint alignment (lat_delay = lateralDelay) is kept — it
  was a genuine pre-existing fix and is exactly correct with the EMA gone.
* lateral timing is now byte-equivalent to 3.2.10/3.3.0e-as-validated for
  stock-override bundles. RESTORE YOUR MODELS-PAGE DELAY to your preferred
  per-model value (e.g. 0.35) — the compensation you added against the
  early-steer artifact is no longer needed.
* radar-tracks work (3.3.0e/3.3.1) carried unchanged.
* chore: FUNNYPILOT_VERSION -> 3.3.2; EXPECTED_VERSION -> 3.3.2;
  code_smoothsec replaced by code_smoothrev (verifies the revert is the
  code actually running).

FunnyPilot v3.3.1 (2026-07-11)
========================
Radar-tracks ENABLE is now evidenced and verified (lateral confirmed good
on 3.3.0e, carried unchanged). The 3.3.0e log showed the radar's OUTPUT but
nothing about the enable handshake itself — and the upstream enable had a
real honesty bug: it fetched the write response with timeout=0, never
checked it, and never read the config back, so "successfully enabled" (and
radarUnavailable=False) could be reported when the radar had NACKed the
write. That produces exactly a log full of lead distances with zero real
track points.

* fix(opendbc enable_radar_tracks): success is now claimed ONLY when the
  post-write read-back shows the tracks bit set. Write ack is checked with
  a real timeout; a NACKed/silent write returns False so radarUnavailable
  correctly stays True (clean stock fallback instead of a dead parser).
* feat: the full handshake is appended to radar_enable.jsonl (web UI ->
  Logs; Verify -> triage_radaren shows the last boot inline): per attempt
  {session answered?, current config hex, write ack?, post-write verify
  hex, enabled}, plus the radar's DEVICE FINGERPRINT read in-session
  (UDS DIDs: application software id 0xF181, part number 0xF187, HKG
  version blob 0xF100) — what's needed to match this DL3 radar against
  known-good tracks configs and pick an alternate payload if 0142 is
  rejected. Exceptions land in the log too. Logging is best-effort and
  can never break the enable itself.
* feat: radard writes a radar_identity record at startup into
  radar_tracks.jsonl: carFingerprint, radarUnavailable (the enable's
  claimed outcome), and ALL ECU firmware versions from openpilot's
  ignition-time FW query (incl. the fwdRadar ECU) — the car-side half of
  the fingerprint.
* chore: FUNNYPILOT_VERSION -> 3.3.1; EXPECTED_VERSION -> 3.3.1; new
  code_radaren + triage_radaren Verify rows.
* tests: NEW opendbc test_enable_radar_tracks.py (5 cases with a scripted
  fake UDS query: verified success; NACKed write MUST fail — the exact
  upstream bug; silent radar; already-enabled short-circuit; unwritable
  log dir never breaks the enable) + 2 identity-record cases. Full suite
  green.
* READING THE LOG: radar_enable.jsonl "session":false => radar never
  answered 0x7D0 (wiring/bus); "write_ack":false or verify unchanged =>
  firmware rejected config 0142 (send the ident block + we try the
  alternate payload next); "enabled":true + radar_tracks.jsonl n>0 =>
  done, tracks are real.

FunnyPilot v3.3.0e (2026-07-10)
========================
EXPERIMENTAL — enables RADAR TRACKS on the 2021+ Kia K5 (DL3) and logs them
to the web UI, groundwork for radar-grounded longitudinal tuning.

* HOW IT WORKS: the sunnypilot base already auto-enables Mando radar
  tracks (opendbc/sunnypilot _initialize_radar_tracks: a UDS config write
  to the radar at 0x7D0 on every ignition, panda-safety allowlisted, retry
  x2) — but only for platforms carrying HyundaiFlags.MANDO_RADAR, and
  KIA_K5_2021 didn't have it. Added the flag: the platform gains the
  hyundai_kia_mando_front_radar DBC, the enable runs at car init, and on
  success the radar broadcasts its raw track table (RADAR_TRACK_500-51f,
  32 slots, 50 Hz) which openpilot's radar interface parses into
  liveTracks. If the radar declines the write, radarUnavailable stays True
  and EVERYTHING behaves exactly as today (vision + SCC lead emulation) —
  graceful, no fault path. The radar's own SCC function is not affected by
  the tracks-output bit (long-established community config).
* STOCK ACC IS ENOUGH FOR LOGGING — openpilot long NOT required: the
  enable runs in card's fingerprint/init path before long-control mode
  matters, the 0x7D0 TX is allowlisted in panda safety unconditionally,
  radard runs onroad regardless of long mode, and liveTracks publishes
  either way. Drive normally on stock ACC and the log fills.
* feat: NEW RadarTracksMonitor (triage_recorder.py) wired into radard —
  1 Hz records in radar_tracks.jsonl (web UI -> Logs): n/nmin/nmax track
  count (n pinned at 0 in traffic = enable didn't take), 3 closest points
  [dRel, yRel, vRel], radarState leadOne/leadTwo [dRel, vLead, aLeadK],
  radar CAN error count. Duck-typed + fully try/excepted: telemetry can
  never take radard down (garbage-input test included).
* Verify gains: code_radartrk (flag present) and triage_radar (last
  radar records inline).
* chore: FUNNYPILOT_VERSION -> 3.3.0e; EXPECTED_VERSION -> 3.3.0e.
* tests: 4 new RadarTracksMonitor cases; opendbc hyundai platform suite
  green with the flag (13 passed, 383 subtests).
* NOTE: first drive, open Verify -> triage_radar or Logs ->
  radar_tracks.jsonl. Healthy = n in the 5-25 range in traffic with
  plausible closest-point distances. n = 0 everywhere means the DL3
  radar firmware rejected the config write — copy the log anyway and
  we'll try the alternate enable payload next.

FunnyPilot v3.2.12 (2026-07-10)
========================
SUPERSEDES 3.2.11 — DO NOT FLASH 3.2.11. Two corrections from user feedback
and a deeper code read:

  (1) The models-page delay knob (0.29-0.39 per model, user-tuned, feeds the
      model network itself via lateral_control_params) is the USER'S — the
      smoothing must adapt to it, never require changing it.
  (2) 3.2.11 edited the WRONG DAEMON for this device: custom Model Manager
      bundles run through sunnypilot/modeld_v2 (NativeProcess
      modeld_tinygrad), which has its own per-bundle smoothing override
      ('lat', default 0). 3.2.11's stock-modeld constant would have done
      nothing there while controlsd's +0.2 misaligned the PID setpoint
      buffer by 0.2 s. Also corrected: latDelay 0.478->0.497 DRIFTS in the
      logs, i.e. it is the live learner's measured value — the K5's true
      command-to-response lag is ~0.5 s.

* feat: delay-funded adaptive smoothing (lat_smooth.smooth_seconds_for_delay):
      tau = clip(0.4 * lateral_delay_in_use, 0, 0.3)
  and the action horizon is sampled EARLIER by tau, so the EMA's lag is paid
  from inside the delay window: effective total ALWAYS equals the configured
  delay. Set the knob higher -> more smoothing time; lower -> less — the
  window is finally "filled with something useful" at any setting. At the
  user's current ~0.5 s in-use delay: tau = 0.2 s.
* modeld_v2 (the daemon this device runs): tau = bundle 'lat' override if
  set, else the adaptive budget; the generation >= 10 EMA gate is respected
  (older bundles get tau = 0 AND no horizon shift, so nothing steers early);
  the NETWORK still receives the user's full delay via
  lateral_control_params — per-model delay tuning is byte-identical.
* stock modeld: same total-preserving scheme (LAT_SMOOTH_SECONDS constant
  now only a zero fallback); controlsd aligns the PID setpoint buffer on
  lateralDelay directly (the old +constant also silently ignored per-bundle
  overrides when modeld_v2 was active — pre-existing misalignment fixed).
* SAFETY PASS (pre-drive review, all paths): budget function total on
  None/NaN/inf/negative/string -> 0.0 (smoothing off, tested); sample
  horizon floored at DT_MDL so curv_from_psis divides by t >= 0.1 with
  v clipped >= 1; np.interp inputs stay well inside T_IDXS; NO new array
  indexing anywhere; smooth_value guards tau <= 0; LatSmoother passes
  through before its first knot, holds on NaN, resets on lat-inactive;
  latcontrol delay_frames clip handles any delay >= 0; modeld_v2 test stub
  covered by getattr fallback; no new Params reads in hot loops.
* chore: FUNNYPILOT_VERSION -> 3.2.12; EXPECTED_VERSION -> 3.2.12;
  code_smoothsec now greps smooth_seconds_for_delay in modeld_v2 (the
  daemon actually running on this device).
* tests: 4 new budget cases (knob scaling, cap, degenerate-input safety,
  budget < delay always); full suite 55 green.

FunnyPilot v3.2.11 (2026-07-10)
========================
One feel change, cleanly A/B-able against 3.2.10: spend the preview window
on smoothing — the user's original "use the artificial delay to interpolate
in realtime" concept, implemented through the mechanism upstream already
plumbed for exactly this.

* WHAT WAS FOUND: the models-page software delay (LagdToggleDelay ~0.35 s,
  used when the live-learning toggle is off: delay = steerActuatorDelay +
  LagdToggleDelay ~= 0.15 + 0.35 = 0.50 s — matching the 0.478-0.497
  latDelay in the triage logs) RESERVES a preview window but nothing ever
  SPENT it: modeld's LAT_SMOOTH_SECONDS EMA — whose lag is pre-paid by
  adding LAT_SMOOTH_SECONDS to both modeld's action horizon and controlsd's
  lat_delay, so total reaction time is unchanged — arrived set to 0.0 via a
  sunnypilot base sync. The window sat idle as pure dead time.
* feat: LAT_SMOOTH_SECONDS 0.0 -> 0.2. The 20 Hz desired-curvature knots
  are now themselves smooth (EMA tau 0.2 s + the existing 2.5 m/s^3 jerk
  clamp), so the deltas that LatSmoother spreads per-frame get smaller and
  more consistent — many small wheel movements instead of a few bites —
  with zero added reaction time (paid from preview, not response).
* RECOMMENDED PAIRING: reduce the models-page software delay 0.35 -> ~0.15
  so TOTAL preview stays ~0.5 s (0.15 hardware + 0.15 software + 0.2
  smoothing); this re-allocates idle dead time into active smoothing.
  If steering feels EARLY (turning in before the curve), reduce it more.
* feat: triage ctx gains latDelayEst (liveDelay.lateralDelayEstimate) next
  to latDelay (the value in use). If est << used, we are steering
  systematically early — evidence for trimming the artificial delay
  further (the suspected contributor to "bite then loosen").
* chore: FUNNYPILOT_VERSION -> 3.2.11; EXPECTED_VERSION -> 3.2.11; new
  code_smoothsec self-check. Lateral interpolation (LatSmoother), torque
  features, override gate, triage recorder all unchanged from 3.2.10.

FunnyPilot v3.2.10 (2026-07-10)
========================
Restores the VALIDATED lateral interpolation feel. The 3.2.9e PlanRider
experiment is deleted after one drive: "feels like 3 updates a second,
two 45-degree bites instead of ten 9-degree ones".

* post-mortem (why PlanRider staircased): the plan-sampling formula
  (2*psi/(v*t) - psi_rate/v) is nearly t-INVARIANT inside a curve —
  advancing the sampling horizon between model frames barely moved the
  output, and each new plan then delivered the entire 50 ms of turn
  progression as ONE step. That is the stock 20 Hz staircase reborn, with
  its biggest steps exactly in sharp turns, grouped by the jerk clamp
  into a few large surges. The idealized-ramp unit tests passed because
  synthetic ramp plans are the one case where riding is smooth; real
  plans are state-anchored and quasi-steady in curves. Lesson recorded:
  smoothness must be guaranteed BY CONSTRUCTION (spread the knot delta),
  not hoped for from a formula's behavior between knots.
* feat: NEW selfdrive/controls/lib/lat_smooth.py — LatSmoother, the
  months-validated 3.1.0e delta/5 schedule in its minimal robust form:
  on each 20 Hz model action, prev <- last OUTPUT, cur <- new action;
  every 100 Hz frame emits prev + clip(elapsed/T_MODEL + 0.2, 0, 1) *
  (cur - prev). Bit-compatible with the validated feel at healthy 100 Hz
  (0.2/0.4/0.6/0.8/1.0 x delta), provably moves EVERY control frame,
  output always inside [prev, cur], continuous at ANY cadence (prev is
  the last output, so early/late knots can never step the command),
  holds cur if the model stalls, NaN-safe. No SETTLE, no lookahead, no
  health blend, no frame counters — 40 lines, nothing left to degrade.
* removed: lat_plan_rider.py + its tests. Dev-UI INTERP gauge and triage
  hmin/havg return to realized control-frames-per-model-frame (5 =
  healthy). Torque-side features and the 3.2.8 override gate untouched;
  triage recorder unchanged.
* chore: FUNNYPILOT_VERSION -> 3.2.10; EXPECTED_VERSION -> 3.2.10;
  code_planrider -> code_latsmooth; code_controlsd greps v3.2.10;
  _FEEL_FILES hashes lat_smooth.py.
* tests: test_lat_smooth.py (9 cases: exact validated schedule,
  moves-every-frame, bracket containment, early/late-knot continuity,
  model-stall hold, 50 Hz cadence independence, NaN hold, re-engage,
  health counting).

FunnyPilot v3.2.9e (2026-07-10)
========================
EXPERIMENTAL — deep reset of the lateral smoothing stack, replacing every
interpolation concept since v3.0.2e with one idea: RIDE THE PLAN.

Hardware findings that motivated keeping this in software (2021 K5 DL3,
Mando/Mobis MDPS): the LKAS torque interface runs at 100 Hz (LKAS11,
STEER_STEP=1) — the SAME rate as the comma's control loop, and the EPS's
internal motor loop is faster still, so there is NO update-rate mismatch.
The real hardware limits are: (1) torque slew caps of +3/-7 counts per
10 ms frame of a 384-count max (full authority takes ~1.3 s to ramp in —
an EPS fault-tolerance constraint, not tunable), (2) modest total assist
authority, (3) a measured ~0.48-0.50 s command-to-response lateral delay
(liveDelay, includes EPS + chassis). None of these are removable in code;
all of them are exactly what delay-aware control is for. Conclusion: the
smoothing problem is legitimate software territory, but the old stack was
solving the wrong formulation.

* feat: NEW selfdrive/controls/lib/lat_plan_rider.py — PlanRider. Every
  approach since 3.0.2e (frame counters, time-anchored knot interpolation,
  PHASE_LEAD, SETTLE ease-outs, lookahead weights, health blends)
  interpolated between 20 Hz POINT SAMPLES of the model's plan. But the
  model publishes its entire smooth plan every frame. PlanRider evaluates
  the plan itself at a continuously advancing horizon:
      t = lat_delay + DT_MDL + (time since the plan was captured)
  The plan segment from t to t+50 ms is by definition what the model wants
  the car doing until the next update — riding it gives per-frame-smooth
  curvature with ZERO added lag (we sample the plan's future, never filter
  its past). Plan handoffs are continuous by construction (successive
  plans are evaluated at the same wall-clock target instant); genuine
  model revisions are bounded by a single 2.5 m/s^3 lateral-jerk clamp —
  the ONLY shaping constant left in the lateral path.
* Properties the old stack needed machinery for, now free: cadence
  robustness (a late model frame is ridden further along the current plan
  — extrapolating the model's own intent — instead of stalling; > 0.2 s
  stale degrades to hold), no lane-change special case (nothing to force
  off), NaN/short plans fall back to the model's action value (= stock).
* removed: lat_interp.py (LINEAR/SETTLE) + its tests + the INTERP_METHOD
  switch + controlsd's _model_lookahead_curv. The /dev/shm/lat_interp
  dev-UI heartbeat and triage hmin/havg fields now carry PlanRider plan
  freshness (5 = fresh, 0 = stale/held) — same scale, same consumers.
  Torque-side features (lane-change scale, re-engage ramp, smooth stop,
  3.2.8 override gate) are untouched.
* note: the pasted triage window for this report happened to cover only
  parked idle (la=0, v=0, tqx=0 throughout) — the drive around the user's
  mark wasn't in the copied tail, so the 3.2.8 gate verdict is still
  open; the recorder keeps running unchanged on this branch.
* chore: FUNNYPILOT_VERSION -> 3.2.9e; EXPECTED_VERSION -> 3.2.9e;
  code_latinterp -> code_planrider check, code_controlsd greps v3.2.9e,
  _FEEL_FILES hashes lat_plan_rider.py.
* tests: test_lat_plan_rider.py (9 cases: exact tracking on constant-
  curvature plans, NO-STAIRCASE invariant on ramps, continuous handoffs,
  jerk-clamped model revisions, late-frame ride-through, stale hold +
  health decay, fallback, bad-plan rejection, reset seeding).

FunnyPilot v3.2.8 (2026-07-07)
========================
Fix for the "bite then loosen" lateral oscillation reported on the first
3.2.7 drive: strong back-and-forth alternation during large steering
adjustments — a hard initial bite, an immediate ~40% loosen, repeating at a
few Hz.

* fix: the v3.2.3st driver-override softening could limit-cycle against the
  controller's own output. It scaled TOTAL steering torque to 60% the
  instant CS.steeringPressed latched — but steeringPressed is just
  torsion-bar torque over a threshold (HKG: 150 counts, 5-frame debounce),
  and a hard steering bite can cross it with NO driver involved: the wheel
  rim's own inertia (or a lightly resting hand) resists the rapid
  acceleration and twists the bar. Full torque -> wheel accelerates ->
  "pressed" -> torque cut to 60% AND PID integrator frozen -> wheel
  decelerates -> bar relaxes -> "pressed" clears -> full torque bites
  again. NEW selfdrive/controls/lib/override_gate.py: the softening now
  engages only after a SUSTAINED press (0.4 s continuous) and releases only
  after a sustained let-go (0.3 s), so it structurally cannot alternate.
  Inertia blips (~0.1-0.25 s) never qualify; a genuine takeover engages
  ~0.4 s in and holds steady through threshold flicker. The driver always
  wins physically regardless — panda driver-torque limits and the EPS are
  untouched, and the takeover-comfort feature is preserved.
* note: this is the hypothesis the 3.2.7 instrumentation was built to test
  (spe/ovr fields). The instrumentation stays on in 3.2.8 — if oscillation
  persists, spe/ovr/sat/slb in lat_interp.jsonl will say what it actually
  is; if it stops, the log will show spe blips with ovr pinned at 1.0
  (gate rejecting them). The reported "5 -> 3" was illustrative, not
  measured; the gate is safe either way because it only ever REDUCES how
  often the softening can engage.
* chore: FUNNYPILOT_VERSION -> 3.2.8; EXPECTED_VERSION -> 3.2.8; new
  code_ovrgate self-check.
* tests: test_override_gate.py (8 cases: the limit-cycle pattern can never
  engage, sustained press engages/holds/releases correctly, per-frame
  toggling cannot cycle the state, reset).

FunnyPilot v3.2.7 (2026-07-05)
========================
Forensics build for the RETURNED "smoothing feels turned off after the car
sits parked" issue (came back after ~8 h parked despite the 3.2.5st
updater-revert guards). This version changes NO control behavior vs 3.2.6e —
it adds a black-box flight recorder so the next occurrence produces evidence
instead of a feeling. Copy the logs from the web UI when it happens; the
root-cause fix ships in the next version once the data says which
hypothesis is real:

  A. CODE SWAP — something still replaces the code while parked/at boot.
     nav_webserver logs a code-identity record at startup and every 10 min
     (branch, commit, dirty flag, FUNNYPILOT_VERSION, UpdaterTargetBranch,
     staged-update branch, .overlay_consistent, boot_id, uptime, and sha1
     hashes of the four feel-defining files: lat_interp.py, long_shaping.py,
     controlsd.py, latcontrol_torque.py). Pulses only write full records on
     CHANGE (pulse-change) — so if a swap happens at 3am while parked, the
     log pins down when, not just that.
  B. RUNTIME DEGRADATION — code fine, but lat_interp loses sub-frame
     headroom or falls into fallbacks. controlsd writes a 1 Hz record while
     onroad: interp health min/avg (5 healthy, <4 degrading — min is kept
     so a transient stall can't be averaged away), lat/long active
     fractions, v_ego, lane-change flag, ISO curvature-clip count, long
     aTarget vs commanded accel.
  C. TUNING DRIFT — same code, different feel: every 10 s the 1 Hz record
     embeds a live-tuning context (torque latAccelFactorFiltered, torque
     friction, angleOffsetDeg, stiffnessFactor, lateralDelay) — if the
     learners moved while parked, it shows here.

* feat: NEW selfdrive/controls/lib/triage_recorder.py — TriageRecorder
  (size-capped rotating JSONL, /data/funnypilot_triage, 4MB + .1 backup,
  all IO best-effort so telemetry can never break controls) +
  LatInterpMonitor (100 Hz samples -> 1 Hz aggregate records).
* feat: web UI "Logs" button — list, view (128K tail), and one-tap COPY of
  every triage log, plus a purple "Mark issue now" button that appends a
  timestamped marker (with optional note) to marks.jsonl so the subjective
  moment can be lined up with the recordings.
* feat: /api/logs, /api/logs/{name}?tail_kb=N, POST /api/logs/mark
  (filename whitelist, no path traversal); Verify gains code_triage +
  triage_boot/triage_lat info rows showing the latest records inline.
* chore: FUNNYPILOT_VERSION -> 3.2.7; EXPECTED_VERSION -> 3.2.7.
* tests: test_triage_recorder.py (10 cases: rotation, 1 Hz cadence,
  min-not-averaged aggregation, context cadence + exception containment,
  name whitelist, hash helper).
* feat (after first 3.2.7 drive): lateral-oscillation evidence for the new
  "bite then loosen" report. The 1 Hz record gains: sp (steeringPressed
  fraction), spe (steeringPressed RISING EDGES per second — a
  driver-override limit cycle is directly countable), ovr (minimum
  driver-override torque scale, 1.0 = off / 0.6 = fully softened = exactly
  the reported 5 -> 3 torque drop), sat (lat controller saturation
  fraction), slb (steer_limited_by_safety fraction), tqx (max |commanded
  torque|). PRIME SUSPECT under test: the v3.2.3st driver-override
  softening — a hard steering bite twists the torsion bar past HKG's
  STEER_THRESHOLD (150, 5-frame debounce) via wheel-rim inertia/resting
  hand, steeringPressed latches, torque is scaled to 0.6 AND the PID
  integrator freezes, the wheel decelerates, pressed clears, full torque
  bites again -> 2-4 Hz limit cycle. spe >= 2 with ovr 0.6 during an
  oscillation event confirms it; fix lands in 3.2.8. Still zero
  control-behavior changes in 3.2.7.

FunnyPilot v3.2.6e (2026-07-05)
========================
EXPERIMENTAL — longitudinal control rewritten around a single-authority
architecture. Design rules, in strict priority order, of what an autonomous
vehicle owes its passengers longitudinally:
  1. Safety is never comfort-limited. The MPC owns the safe-following problem
     (headway, braking envelope, danger-zone constraint, FCW); nothing
     downstream may weaken or delay its braking. Heuristics may shape its
     INPUTS (cruise speed, headway) but never clamp its output.
  2. Comfort is enforced in exactly one place: a single asymmetric jerk
     shaper. Throttle applies gently; braking slew scales with the demanded
     deceleration; FCW bypasses shaping entirely.
  3. Predictability: set speed means set speed; the command is a
     deterministic function of the plan.
  4. Robustness lives in the speed domain: lead flicker/departure handling
     can hold the car back but can never brake it.

* feat: NEW selfdrive/controls/lib/long_shaping.py — AccelJerkShaper (up-jerk
  1.4-2.5 m/s^3 by personality; down-jerk 4 m/s^3 for mild demands scaling
  continuously to 12 m/s^3 at -3.5 m/s^2, so hard braking is executed near-
  unshaped) and LeadGrace (on losing a lead we were actually following for
  >= 1 s: hold cruise at the lead's last speed 1.5 s, ramp out over 2 s; cap
  floored at v_ego so it can never command braking).
* fix(SAFETY): removed the FollowingControllerV2 accel/jerk overrides. Its
  0.5 m/s^3 "normal" jerk cap applied to the FINAL output could delay a
  3 m/s^2 braking demand by several seconds while its TTC tiers escalated,
  and its tier logic fought the MPC's own (correct) solution to the same
  problem. Following is now owned solely by the MPC.
* feat: follow distance is a constant TIME headway per personality
  (aggressive 1.25 s / standard 1.60 s / relaxed 2.05 s, +0.35 s cushion
  below city speeds) replacing the 3.2.5st speed-indexed tables that were
  most cautious where risk is lowest (3.75 s at city speed, 1.5 s at
  highway speed). COMFORT_BRAKE 2.0 -> 2.2 (earlier-than-stock brake
  initiation WITHOUT the closing-rate obstacle inflation hack, which
  double-counted braking distance and caused early/phantom braking).
  STOP_DISTANCE 11 -> 7.5 m (roomier than stock 6 m, no longer invites
  cut-ins). Relaxed personality now also gets a higher MPC jerk cost (2.0).
* fix(predictability): removed the hidden 0.9x cruise offset (car now
  actually drives the set speed), the 4 s personality-switch gas gate (the
  MPC's accel-change cost already smooths headway transitions), the lead-cap
  blend, and the asymmetric output filter (instant-down/0.35 s-up) — all
  replaced by the one shaper stage.
* feat: longcontrol.py — bumpless transfer: entering PID from
  stopping/starting seeds the integrator so the first frame continues from
  the last commanded accel instead of stepping; the starting state slews
  toward startAccel at 6 m/s^3 instead of stepping (kills the launch
  head-snap, still fast enough to release brake-hold).
* chore: deleted sunnypilot/.../long_v2/following_v2.py; SP planner keeps
  only the speed-domain governors (SCC-V/SCC-M/SLA/weather/road caps).
* test: NEW selfdrive/controls/lib/tests/test_long_shaping.py (22 cases:
  jerk asymmetry, strong-braking-barely-delayed safety invariant, FCW
  bypass, NaN containment, LeadGrace arm/hold/release/never-brake
  invariants); test_longcontrol.py gains bumpless-entry and starting-ramp
  cases. All import-light, run without the full openpilot env.
* feat: Speed Limit Assist rewritten ("the cluster set speed IS the SLA
  target") to fix the dynamic-offset carryover and the activation jerk:
  - Tap-to-adopt activation: with SLA armed (mode = Assist, long engaged —
    no prompts, no auto-activation), a SHORT cruise-down tap activates SLA
    and ADOPTS the current set speed unchanged: going a set 50 mph in a 45
    zone -> active at +11%, zero speed change. The tap is swallowed in
    cruise.py so it no longer also decrements the set speed. Long presses
    remain ordinary speed adjustments and never activate SLA.
  - Offset %% carryover between zones, working: 60 set in a 50 zone = +20%;
    a 30 zone becomes 36. Manually dropping to 33 re-locks the ratio at
    +10%; the next 40 zone becomes 44. ROOT CAUSE FIX: the old cruise
    helper snapped the set speed to the RAW limit on every zone change
    (it never knew the ratio), and SLA then recomputed the ratio from that
    snapped value -> ratio wiped to ~0 at every zone boundary. The helper
    now snaps to limit*(1+ratio) (reads slaDynamicOffset from
    longitudinalPlanSP), which makes SLA's recompute-from-cluster
    idempotent — manual button taps and our own snaps use one code path.
  - Deactivation only on longitudinal disengage or turning the mode off
    (ratio resets). Losing the speed limit source holds the last known
    zone. The preActive confirm flow, CST thresholds and pending state are
    gone; the SLA gas-gating accel path (dead since 3.2.5st — published
    but never consumed) is removed per the single-authority rule: SLA is
    speed-domain only, braking into a lower zone is the MPC's job.
  - tests: test_speed_limit_assist.py rewritten import-light (17 cases,
    including both examples above, snap idempotency, clamp at +/-50%,
    long-press/stale-tap rejection).
* fix(SCC-V, SCC-M): both v2 curve controllers were DEAD CODE — they have
  never activated. SCC-V sampled the removed `lateralPlan` service (the
  read threw every frame; the handler returned 0 predicted lateral accel),
  and SCC-M parsed MapTargetVelocities as {v, dist, radius} when mapd
  writes [{latitude, longitude, velocity}, ...]. The only live curve logic
  was the legacy v1 SCC, whose output the LongV2 governor discarded.
  Rewritten:
  - SCC-V reads modelV2 (orientationRate.z x velocity.x, the proven V-TSC
    signal) and does pointwise corner-speed planning over the plan
    horizon: for each point, corner speed v*sqrt(a_lat_limit/a_pred) plus
    a 1.2 m/s^2 approach-decel budget scaled by time-to-corner. The
    braking point falls out of the math — no ENTERING/TURNING tier
    machine. In-corner it holds corner speed until the plan flattens.
  - SCC-M parses the real mapd route data (nearest-point + forward slice,
    vectorized haversine, 400 m lookahead) and applies a constant-decel
    envelope sqrt(v_curve^2 + 2*a*d) with a 2 s early-arrival buffer —
    replacing jerk-integral braking math that also had a broken quadratic
    root (`/ 2 * a` multiplied by a/2). Straight-road map points are
    never trimmed into constraints.
  - shared long_v2/curve_cap.py: debounced activation (2 frames), cap
    seeded at current speed (no step), fast down-tracking, 2.5 m/s^2-rate
    release, clean deactivation. Both controllers are speed-domain
    governors only — the MPC + shaper own the actual deceleration.
  - honest physics: lat-accel target is now a comfort constant (2.4
    m/s^2, LongV2Tuning `a_lat_target`) with the friction estimate
    bounded to +/-30% influence — `liveParameters` friction is a steering
    -model parameter, not road grip; the old k*fric*g formula demanded
    5.6 m/s^2 lateral before acting. New `sccm_speed_trim` (0.95) trims
    mapd curve speeds directly. k_sccv/k_sccm remain parseable but dead.
  - controllers now honor the SmartCruiseControlVision/Map toggles; the
    legacy v1 SCC (computed, discarded) is removed from the SP planner.
  - tests: new long_v2/tests/test_scc_v2.py (16 cases: activation
    envelopes, approach tightening, in-corner hold, release, passed-curve
    and no-data handling, bounded fric influence, cap seeding/debounce).
* chore: FUNNYPILOT_VERSION -> 3.2.6e; /api/diagnostics EXPECTED_VERSION ->
  3.2.6e, new code_longshape / code_longplan / code_sla / code_sccv2
  self-checks.

FunnyPilot v3.2.5st (2026-07-04)
========================
Root-cause fix for "interpolation feels disabled after the device sits offroad,
reboot doesn't help, only a web-page reflash (~9MB) brings it back".

* fix: The stock openpilot updater was SILENTLY REVERTING the flashed code. The
  chain: the web-UI flash does a plain `git checkout` in /data/openpilot but
  never updates the updater's `UpdaterTargetBranch` param, which still points at
  an older branch from a previous install. While the device sits offroad,
  `updated` runs every ~1.5 h; on a metered hotspot it skips fetching UNTIL its
  3-day timer expires (why it only happens after sitting long enough), then it
  git-fetches the stale target branch (the same ~9MB class of traffic seen on a
  reflash), checks it out in an overlay, and "finalizes" it into
  /data/safe_staging. On the next soft-off -> boot, launch_chffrplus.sh swapped
  /data/openpilot for that finalized copy WITH NO BRANCH CHECK — interpolation
  gone. Reboots can't fix it (the wrong code is now what's installed); a reflash
  fixes it only because it resets the code and its .git mtimes block the swap
  until the updater re-arms, so the cycle repeated. Also explains why "auto
  update off" didn't help: the sunnypilot "Disable Updates" toggle only persists
  if the reboot dialog is CONFIRMED (cancel silently reverts it), and even a
  pre-staged update would still have been installed by the boot swap.
  Fixed in three independent layers (any one alone stops the revert):
  - launch_chffrplus.sh — boot-time branch guard: a finalized staged update is
    only installed if it is on the SAME git branch as the currently flashed
    code; otherwise it is discarded (`.overlay_consistent` removed) and a
    message is logged. Branch switches now only ever happen via an explicit
    flash (web UI or ssh), never via the boot swap. NOTE: this intentionally
    makes the sunnypilot settings-menu branch selector unable to switch
    branches on this fork — use the web UI Flash button instead.
  - system/updated/updated.py — target self-heal: on startup, if the checked
    out branch is a funnypilot-* branch and `UpdaterTargetBranch` differs, the
    param is rewritten to the flashed branch, so the updater can only ever
    stage the code that is already installed. Also, if the target branch does
    not exist on the `origin` remote, the fetch is skipped cleanly instead of
    failing forever (which would eventually raise the connectivity-needed
    offroad alerts that can block engagement).
  - sunnypilot/navd/nav_webserver.py — the /api/flash endpoint now writes
    `UpdaterTargetBranch` to the branch being flashed and wipes
    /data/safe_staging (unmounting the updater overlay first) before rebooting,
    so a previously staged wrong-branch update can't be installed by the very
    reboot the flash triggers.
* feat: /api/diagnostics ("Verify" button) now surfaces the revert vector:
  `updater_target` FAILS if UpdaterTargetBranch points at a different branch,
  `staged_branch` WARNS if a different branch is staged in /data/safe_staging,
  `updater_off` reports whether DisableUpdates is actually set (catches the
  cancelled-reboot-dialog trap), and new `code_bootguard` / `code_updtarget`
  self-checks verify the two guards are present in the running code.
* fix: "Working tree unmodified" Verify check no longer fails on the on-device
  runtime artifacts `.nav_secrets` and `.funnypilot_nav_cache` — they are not
  shipped code and are now listed in .gitignore, so `git status --porcelain`
  (what the check runs) ignores them.
* chore: FUNNYPILOT_VERSION 3.2.3st -> 3.2.5st (3.2.4e never bumped the file);
  /api/diagnostics EXPECTED_VERSION/branch checks -> 3.2.5st. No control-path
  changes: the 3.2.4e interpolation/smoothing (lat_interp SETTLE + jerk-limited
  model action smoothing) carries over byte-identical.

FunnyPilot v3.2.3st (2026-06-26)
========================
Stable cut of 3.2.2 with three follow-up fixes from on-road feedback.

* fix: Smooth lane changes restored. The v3.2.2 SETTLE ease-out leads the curvature
  as it flattens, which sharpened the S-shaped lane-change path and made lane
  changes feel sharp/jerky vs the smooth 3.2.1st feel. SETTLE is now forced OFF
  during a lane change (model laneChangeState != off) so the maneuver uses the
  plain linear ramp again — identical to 3.2.1st. Normal-driving SETTLE smoothing
  is unchanged, and the soft-lane-change TORQUE scaling in latcontrol_torque.py was
  always untouched. (selfdrive/controls/lib/lat_interp.py — new `lane_change` arg;
  controlsd.py passes it.)
* feat: Driver-override softening — easier to retake the wheel by hand. The v3.2.2
  interpolation sends firmer, more consistent curvature commands, so manually
  pushing the wheel away met more resistance. When the driver is actively applying
  torque (CS.steeringPressed), the TOTAL output torque now ramps down to 60%
  (_OVERRIDE_MIN_SCALE), via a FirstOrderFilter so there's no step in or out, and
  an exact no-op (100%) when not pressing. The interpolation/smoothing is untouched
  and the panda's hardware torque limits remain the safety backstop. Tune
  _OVERRIDE_MIN_SCALE in latcontrol_torque.py if you want lighter/heavier override.
* fix: Brake-with-lead disengage chime silenced (2021 Kia K5 GT, STOCK
  longitudinal). Root cause: the K5 is a classic-CAN button-cancel car, and when
  you brake to disengage, the brake pedal ALREADY cancels the factory cruise — but
  openpilot also spams a redundant CLU11 CANCEL, and doing that while the SCC was
  following a lead is what makes the car chime (no lead -> no chime, matching the
  report). The redundant CANCEL is now suppressed WHILE the brake is pressed; the
  instant the brake releases, CANCEL resumes if cruise is somehow still enabled, so
  cruise can never get stuck engaged. (opendbc_repo/.../hyundai/carcontroller.py)
  VERIFY ON-DEVICE: confirm braking still reliably cancels cruise (it does so via
  the pedal natively); if the chime persists it is coming from a different layer —
  report back and we'll re-trace with a CAN log.
* chore: FUNNYPILOT_VERSION -> 3.2.3st; /api/diagnostics EXPECTED_VERSION/branch
  bumped, plus new `code_override` and `code_chime` self-checks and `code_controlsd`
  now greps `v3.2.3st`. New test_lane_change_forces_linear in test_lat_interp.py.

FunnyPilot v3.2.2 (2026-06-26)
========================
* fix: Interpolation no longer silently degrades after offroad/reboot. Root cause:
  the old interpolation COUNTED control frames and assumed exactly 5 per model
  frame (100 Hz / 20 Hz). When controlsd's effective rate drifts toward the model
  rate under thermal/CPU load — which builds up the longer the device runs, i.e.
  exactly after an offroad/reboot cycle rather than after a cool fresh flash — the
  frame counter stopped advancing and the steering command FROZE at ~20% of every
  model step (the "big bites / feels deactivated" symptom). The interpolation is
  now TIME-ANCHORED: the sub-frame phase is real elapsed wall-clock time over the
  model's fixed 20 Hz period, so it reaches the model's desire on time at any loop
  rate and can't stall. A health term blends toward the model's raw desire when
  sub-frame headroom is lost, so the worst case degrades to STOCK openpilot —
  never to a laggy stall. The healthy-100 Hz feel is bit-compatible with 3.1.0e+.
  (new selfdrive/controls/lib/lat_interp.py; selfdrive/controls/controlsd.py)
* feat: New SETTLE interpolation method, now the DEFAULT. Full responsiveness INTO
  a corner (mathematically never below the linear path — no added turn-in lag),
  then a gentle ease-out as the model's desired curvature flattens toward the
  apex/exit, so the wheel SETTLES instead of arriving in a 20 Hz jerk impulse
  (the "whiplash"). The "is it flattening?" decision uses a one-model-step
  lookahead sampled from the model's OWN published plan (orientation /
  orientationRate) — free compute available inside the actuator/software-delay
  window. Guaranteed to never sit outside the model's [prev, cur] desire bracket
  and to equal the model exactly at each model frame (the model stays the
  reference). Falls back to linear below ~15 mph and on any invalid lookahead.
  Set `INTERP_METHOD = LINEAR` in controlsd.py for the plain validated delta/5
  feel (A/B by flashing, per the fork's pin-in-code convention).
* feat: Honest INTERP health indicator. controlsd writes the REALIZED
  control-frames-per-model-frame to /dev/shm/lat_interp (≈5 healthy, lower =
  losing sub-frame headroom, 0 = paused) instead of a constant "5" heartbeat.
  The dev-UI element colors it green ≥4 / orange 2–3 / red 1, so a degradation is
  now VISIBLE on-device instead of masked by an always-green "5".
  (selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py)
* test: New selfdrive/controls/lib/tests/test_lat_interp.py proves the safety
  invariants (in-bracket / never outside model desire, reaches cur at the model
  frame, SETTLE never below linear, linear == old delta/5 schedule, cadence
  independence, degraded-beats-old-20%-stall, NaN/re-engage/low-speed contained).
* note: A SECOND, device-side cause of "works after reflash, reverts after offroad"
  is possible and is NOT fixable in this repo: the openpilot updater can fetch a
  different `UpdaterTargetBranch` over offroad wifi and swap it in on reboot.
  Confirm on-device with `cat /data/params/d/UpdaterTargetBranch` and
  `grep -c v3.2.2 /data/openpilot/selfdrive/controls/controlsd.py` (the web-UI
  "Verify" button now checks both the version and the new lat_interp module). If
  the running code isn't 3.2.2 after a reboot, the updater reverted it — re-flash
  funnypilot-3.2.2 and check that the updater target branch matches.
* chore: FUNNYPILOT_VERSION → 3.2.2; /api/diagnostics EXPECTED_VERSION, branch
  check, and code markers updated (controlsd grep `v3.2.2`, new `lat_interp`
  module check) so the on-device self-check stays green on this branch.

FunnyPilot v3.2.1st (2026-06-21)
========================
* tweak: More aggressive post-blinker lateral re-engage (stable cut of 3.2.1e).
  - Settle hold shortened 1.0 s → 0.67 s: after the blinker turns off, lateral
    resumes once the wheel has been held within ±20° of center (threshold
    unchanged) for 0.67 s instead of 1.0 s. Brief center crossings still reset the
    timer. (sunnypilot/.../blinker_pause_lateral.py — UNWIND_SETTLE_TIME)
  - Re-engage torque ramp now starts at 15% (was 0%) and reaches 100% over 3 s
    (was 4 s), so steering authority returns sooner without snapping to full
    torque. (latcontrol_torque.py — _REENGAGE_RAMP_START, _REENGAGE_RAMP_DUR)
* note: Stable release based on funnypilot-3.2.1e. All other 3.2.1e behavior —
  fixed 5-way interpolation + INTERP heartbeat and the "Verify" diagnostics modal —
  carries over unchanged. FUNNYPILOT_VERSION → 3.2.1st; /api/diagnostics
  EXPECTED_VERSION and branch check updated to match so the on-device self-check
  stays green on the stable branch.

FunnyPilot v3.2.1e (2026-06-20) [EXPERIMENTAL]
========================
* change: Interpolation is now FIXED at 5-way uniform slicing, unconditionally.
  The dynamic-n corner-intensity gauge and the dCRV (delta-curvature) machinery
  were removed entirely. The control feel is unchanged from 3.1.x (it was always
  uniform delta/5); this just deletes the now-dead gauge math. _MP_MAX_DELTA /
  _MP_HOLD_DECAY / n_raw / n_held are gone.
* change: dev-UI "dCRV" element removed. "INTERP" now reads a constant 5 (green)
  while lateral is engaged and 0 (white) when the interpolation path is paused —
  it is now a live "interp alive" indicator, written to /dev/shm/lat_interp at the
  20 Hz model rate as a heartbeat (useful for spotting if interp ever stops).
* feat: Blinker-unwind re-engage torque ramp. When the blinker-pause feature
  releases lateral control, total torque ramps CONTINUOUSLY 0% → 100% over 4 s —
  recomputed every 100 Hz control frame as a smooth linear function of elapsed
  time (not stepped quarters) — instead of snapping to full authority. Scoped to
  blinker pauses only (a blinker seen while inactive) — a plain engage or
  standstill release still gets instant authority. (latcontrol_torque.py)
* feat: Web UI "Verify" button — a one-tap code-verification modal (styled like a
  test suite, PASS/FAIL/WARN pills) that runs read-only on-device checks (git
  branch/HEAD, working-tree-unmodified, our code markers present, INTERP heartbeat,
  model bundle, updater staging/overlay) and a "Copy Output" button to paste the
  report straight back into Claude. New POST /api/diagnostics endpoint.
  (sunnypilot/navd/nav_webserver.py, sunnypilot/navd/nav_web/index.html)
* fix: Blinker-unwind no longer re-engages mid-S-curve. The wheel must now stay
  within ±20° of center CONTINUOUSLY for 1.0 s (UNWIND_SETTLE_TIME) before lateral
  resumes; briefly passing through center on the way to the opposite lock resets
  the timer. (sunnypilot/.../blinker_pause_lateral.py)
* note: Suspected cause of "interp feels like it deactivates after a few days"
  documented for on-device diagnosis (likely the openpilot updater touching the
  working tree on nightly wifi, or a model-bundle change). The new INTERP
  heartbeat helps confirm whether the interp path is live.

FunnyPilot v3.1.2 (2026-05-31)
========================
* tweak: INTERP gauge tuning (display only — control still uniform delta/5).
  - Threshold _MP_MAX_DELTA 0.00006 → 0.000051 (15% lower), so a 15% gentler
    curve now registers the same INTERP value as before.
  - Default/resting INTERP raised to 3 (n floor 1 → 2).
  - Max INTERP raised to 10 (n cap 6 → 9).
  - Colors rescaled for the 3–10 range: green 3–5, orange 6–8, red 9–10.
  NOTE: Since 3.1.0e the INTERP number does not size the steering control
  (that is always uniform delta/5); these changes affect the readout only.

FunnyPilot v3.1.1st (2026-05-31)
========================
* tweak: INTERP gauge now ranges 1–7 (was 1–5). n_interp cap raised 4 → 6 so
  sharp curves register the full range instead of saturating. Colors: white = 1,
  green = 2–4, orange = 5–7. (Display gauge only — control still uses uniform
  delta/5 slicing from 3.1.0e.)
* tweak: dCRV display scaled ×100 — 0.000637 now reads "0.0637" instead of
  "0.0006". Drops two wasted leading zeros and surfaces two more digits of
  resolution within the same width.
* Stable release built on the validated 3.1.0e uniform-interpolation feel.

FunnyPilot v3.1.0e (2026-05-31) [EXPERIMENTAL]
========================
* fix: Interpolation now actually slices finely. Root cause of "still feels like
  big infrequent bites": the dynamic-n schedule, in the common case (n_interp=1,
  shown as "2" in the UI), built [prev, prev+0.5·delta, cur, cur, cur] — i.e. two
  coarse half-delta jumps in 20 ms then 30 ms flat. The dev-UI value was correct
  but the control was only using 2 of 5 frames at half-steps.
  Control now ALWAYS spreads each model step uniformly across all 5 control frames
  (delta/5 per frame), reaching the new target at the final sub-frame. 2.5x finer
  steering steps in normal driving for the same ~zero added lag.
* note: The dynamic n_interp value is retained purely as the dev-UI corner-intensity
  gauge (it no longer sizes the control steps). _MP_MAX_DELTA / _MP_HOLD_DECAY now
  only affect that display.

FunnyPilot v3.0.9e (2026-05-31) [EXPERIMENTAL]
========================
* feat: Soft lane changes — scale the TOTAL steering torque (feedforward +
  correction) during a lane change instead of only the PID correction. The
  previous design kept the feedforward at 100%, but the lane-change motion
  itself lives in the feedforward, so the maneuver was never actually softened
  — only the error tracking around it. Now the whole motion eases in.
* feat: Floor of 45% — total torque never drops below 45% of demand during the
  maneuver, so corners aren't lost if you signal mid-curve. Blinker ON ramps
  45% → 100% over 6 s; blinker OFF holds 45% for 0.5 s then 45% → 100% over 2 s
  (alpha² ease-in). The old 0% dead zone is removed (it would mean zero steering
  authority under total scaling).
* refactor: Removed the FF/correction split entirely (and with it the NNLC
  torque-space special-casing from 3.0.8e) — no longer needed with total scaling.

FunnyPilot v3.0.8e (2026-05-31) [EXPERIMENTAL]
========================
* fix: Lane change torque split now preserves the ACTUAL feedforward. The split
  previously isolated the "correction" by subtracting a linear-feedforward torque
  even when NNLC produced the output from its own (different) neural feedforward.
  During a lane change at low scale this swapped most of the neural feedforward
  for the linear one — invisible behind a lead (the two agree in steady following)
  but rough on open road (they diverge), which is exactly why lane changes felt
  smooth only with a lead. Now decomposes via the PID's own F vs P+I+D in native
  units, correct for both the neural (torque-space) and linear (lat-accel) paths.
  Exact no-op at scale 1.0, so normal driving is unchanged.

FunnyPilot v3.0.7 (2026-05-31)
========================
* feat: Interpolation peak-hold with decay. n_interp now snaps up instantly on
  a large model-desire delta but decays slowly (~0.15 levels/gate, 5→2 over ~1s)
  instead of dropping the instant the per-gate delta shrinks. Keeps fine
  interpolation through the body of a corner, not just at entry — fixes the
  "interp drops back to 2 while I'm still mid-corner" feel.
* tweak: Kept the model-desire delta as the interpolation trigger rather than
  measured steer angle. The model desire is forward-looking (pre-compensated for
  lat_delay); measured angle is the lagged response and would react to bites that
  already happened. Decay-hold addresses the "feels delayed" symptom instead.
* tweak: INTERP developer UI value now displays a 1-second rolling peak (matching
  dCRV) so the value it's hitting is readable instead of flickering.

FunnyPilot v3.0.6 (2026-05-31)
========================
* tune: _MP_MAX_DELTA 0.000033 → 0.00006 (midpoint between 3.0.4e and 3.0.5e).

FunnyPilot v3.0.5e (2026-05-31) [EXPERIMENTAL]
========================
* fix: Replace speed-scaled torque threshold with a fixed curvature threshold
  of 0.0001 rad/m per step. The v² denominator was making max_step larger than
  the observed deltas at city speeds, causing n_interp to stay at 1 regardless
  of curve sharpness. With the fixed threshold, display climbs 2→5 across the
  observed delta range (~0.0001 straight, ~0.0004 sharp curve).
* fix: Replace Unicode Δ with ASCII "dCRV" in developer UI — Δ rendered as
  "?" due to missing glyph in the on-device font.

FunnyPilot v3.0.4e (2026-05-31) [EXPERIMENTAL]
========================
* tune: _MP_MAX_STEP 0.10 → 0.04. Lower threshold means n_interp climbs
  sooner and more often — display will show 3–5 on moderate curves rather
  than staying at 2 for most of the drive.

FunnyPilot v3.0.3e (2026-05-31) [EXPERIMENTAL]
========================
* feat: Dynamic interpolation step count. Steps per model gate scales with
  the curvature delta — each step bounded to ≤10% torque-equivalent
  (speed-scaled via LAF=2.750). 5-value schedule pre-built per gate.
  n=0 → display 1 (direct jump). n=1 → display 2 (normal). n=4 → display 5.
* feat: INTERP N in developer UI bottom bar (torque controller only).
  IPC via /dev/shm/lat_interp written by controlsd at model-gate rate.
  Green=2–3, Orange=4–5, White=1. Zero compilation required.
* tweak: Post-blinker correction ramp is now quadratic ease-in (alpha²).

FunnyPilot v3.0.2e (2026-05-28) [EXPERIMENTAL]
========================
* fix: Removed Layers 1 and 2 from lateral controller — the FirstOrderFilter
  on feedforward and the setpoint averaging window both introduced phase lag
  that caused outward drift on curves. Setpoint lookup restored to direct
  single-point: lat_accel_request_buffer[-delay_frames].
* feat: Midpoint interpolation in controlsd. Between consecutive model frames
  (20 Hz) a single mid-frame command is injected at controlsd frame index 2 of 5
  (10 ms after the model frame) equal to (prev_frame + cur_frame) / 2. Effective
  lateral command rate rises from 20 Hz to ~40 Hz with zero added lag: no filter,
  no phase shift — just geometry. When latActive is False the state resets so
  there is no stale value on re-engagement.

FunnyPilot v3.0.1 (2026-05-28)
========================
* fix: latAccelFactor (LAF) locked at 2.750. Live torque calibration updates
  latAccelOffset and friction normally but LAF is pinned, giving consistent
  steering feel regardless of torqued's current estimate.
* tweak: Smooth stopping interpolation confirmed 0–15 mph (100% torque at 15 mph).
* feat: Web UI branch selector rebuilt as a native scrollable list — no more
  native <select> overflowing the screen on mobile. Bottom-sheet modal, touch
  targets ≥44 px, minimal dark theme. Flash button disabled until a branch is
  selected; status feedback inline.

FunnyPilot v3.0.0e (2026-05-28) [EXPERIMENTAL]
========================
* note: LAT_SMOOTH_SECONDS held at 0.0 (reverted from 0.1) pending validation —
  the shared constant has complex interactions with NNLC's desired_lat_jerk_time
  that need further testing before enabling.
* exp: Layer 1 — Feedforward smoother. The curvature-driven feedforward term
  (path/corner demand passed to the PID) is passed through a FirstOrderFilter
  with time constant dynamically set to max(lat_delay, 0.1 s) each frame. Sudden
  path model updates are spread over the vehicle's own response window. Friction
  compensation is added AFTER the filter so it remains fully responsive to
  direction changes. Filter is seeded from current ff on re-engagement to prevent
  torque spikes.
* exp: Layer 2 — Setpoint averaging. The single delay-point lookup is replaced
  with a mean over a ±(delay/3) window centered on the delay point. Single-frame
  error spikes are suppressed without time-shifting the setpoint.
* Sanity-checked: DM-disabled forceDecel defaults to 0.0 (safe), isRHD defaults
  to False (correct for US/LHD). No blocking issues found.

FunnyPilot v3.0.0 (2026-05-28)
========================
* feat: Driver monitoring silenced — processes kept running (required for UI
  data flows and driverStateV2 stability) but all timeouts set to 86400 s (24 h)
  so DM never reaches alert state. Events also blocked at selfdrived level as a
  second layer of protection. No beeping, no alerts, no disengagement.
* feat: Corner-aware lane change torque. Output torque is now split into
  feedforward (corner demand) and correction (PID error) components. Only the
  correction is reduced during a lane change; the feedforward always runs at
  100%, ensuring the car never applies less torque than the curve requires and
  cannot slip to the outside of a turn. Correction starts at 10% on blinker
  onset and ramps linearly to 100% over 6 seconds.
* feat: Post-blinker settle sequence. When the blinker turns off, correction
  torque drops to 0% for a 0.5 s dead zone (letting the steering unwind
  without controller fight), then ramps from 0% back to 100% over 2 seconds,
  preventing the abrupt full-torque snap back to lane-center the moment the
  wheel realigns.

FunnyPilot v2.0.5 (2026-05-08)
========================
* tweak: Reintroduced the hidden −10% cruise offset strictly when cruise is the
  sole limiter, leaving UI and PCM speeds untouched while smoothing positive
  acceleration transients via a first-order filter.
* tweak: Lane change torque ramp now preserves the torque present at signal
  onset, preventing understeer on curved roads while still easing back to full
  authority over five seconds.
* fix: Follow distance tables expanded ~50% across all personalities, with
  inflated lead obstacles and longer stop distance for safer approach behavior.
* fix: Improved blend between free-cruise and lead-governed targets to remove
  longitudinal oscillations when a lead drops in and out of view.
* fix: Experimental-mode cruise initialization now honors current vehicle speed
  instead of defaulting to 65 mph.
* fix: Speed Limit Assist keeps the user-selected offset across zones and begins
  gas gating earlier with stronger decel bias to meet new limits smoothly.

FunnyPilot v2.0.4 (2026-05-08)
========================
* revert: Longitudinal planner, MPC tuning, and LongV2 controllers restored to
  the v2.0.0 state after regression reports in 2.0.1/2.0.2.
* fix: Web terminal now spawns the user's login shell (outside the venv) while
  automatically removing `VIRTUAL_ENV` so Git automation works without manual
  cleanup.
* fix: SCC-V / SCC-M debug badges always render their governing speed in MPH
  when Developer UI is enabled, even when the controllers are inactive.

FunnyPilot v2.0.3 (2026-05-08)
========================
* fix: Cruise offset now only applies when the planner is free-cruising on the
  user set speed. Lead vehicles, map caps, and speed limits retain full
  authority, preventing runaways once external constraints clear.
* fix: Browser terminal launches outside the openpilot virtualenv and flash
  scripts execute with a clean PATH, so git branch discovery and updater flows
  work reliably again via the web UI terminal.

FunnyPilot v2.0.2 (2026-05-06)
========================
* fix: Critical safety bug — runaway acceleration with no lead vehicle
  Root cause: V_CRUISE_UNSET (255 kph) is the sentinel for "cruise not
  set yet." min(255, V_CRUISE_MAX=145) = 145 kph = 90 mph was flowing
  through to the MPC uncapped because the -10% offset only applied when
  initialized. MPC then chased 90 mph with no lead to constrain it.
  Fix: when v_cruise_initialized = False, use v_ego (hold current speed)
  instead of V_CRUISE_MAX.
* fix: Remove lead-lost holdout from FollowingControllerV2. The 1.2s
  hold + 2.0s ramp on lead disappearance caused edge cases on cold-start
  (no lead → immediate cap at v_ego=0 for several seconds). The MPC
  handles lead-loss transitions naturally via its own safe-distance
  constraint. When no lead is detected, all caps/overrides clear instantly.

FunnyPilot v2.0.1 (2026-05-06)
========================
* Lead following — closing-rate brake curve:
  - Replaced TTC-only tier triggering. With small Δv (e.g. 50→42 mph) the
    old TTC stayed >10s until you were right on top of the lead, so the
    controller did nothing and the MPC coasted too late and too long.
  - New: required-decel tiers based on `(v_ego² − v_lead²) / (2·Δd_to_gap)`.
    Pre-empts the coast and engages gentle decel as soon as we're closing.
* Stronger stopping power:
  - LongV2 `decel_comfort` 1.8 → 2.5 m/s², added `decel_max` 3.5 m/s² for tier 4
  - `jerk_limit_normal` 0.5 → 0.7 m/s³, `jerk_limit_safety` 3.0 → 4.5 m/s³
  - MPC `COMFORT_BRAKE` 2.0 → 1.5: enlarges safe-distance constraint so MPC
    starts braking sooner when closing on a slow lead
* SCC badges in debug UI mode:
  - When DevUIInfo is enabled, badges show the calculated v_target at all
    times (not only when actively governing). SCCVisionV2 / SCCMapV2 now
    always publish their computed corner speed.
* Friction coefficient recalibration (matches observed 0.08 — 1.1 range):
  - Nominal dry pavement μ = 0.5 (was 0.8)
  - Wet/snow boundary at μ = 0.092 (was 0.6)
  - `comfort_scale` linear ramp from 0.5 (wet floor) to 1.0 (dry nominal)
  - `weather_speed_scale` ramps 0.6 → 1.0 across the wet range
* Hidden −10% cruise speed offset:
  - The speedometer / set-speed UI is unchanged (reads `carState.vCruise`
    directly), but everything downstream of the planner sees `v_cruise * 0.9`.
  - Setting cruise at 50 mph → MPC plans for 45 mph.
* Post-blinker unwind (ported from 1.0.9):
  - Lateral control stays paused after the blinker turns off until the
    steering wheel returns within 20° of center.
  - Hardcoded ON for this personal branch (no params toggle).
* Driver monitoring restored to 9× timeouts (matches 1.0.8.3):
  - Passive wheel-touch: 270s (was 90s)
  - Active monitoring: 99s (was 33s)
* Smooth lane change torque (blinker-triggered):
  - On blinker rising edge, lateral torque drops to 25% (75% reduction) and
    linearly ramps back to 100% over 5.0 seconds.
  - Trigger is purely the blinker — ramp begins the moment the user signals,
    not when the model commits curvature.

FunnyPilot v2.0.0 (2026-05-06)
========================
* LongV2 — physics-based longitudinal control:
  - FRIC-aware corner speed: v_corner = k × sqrt(μ × g × R) using live friction coefficient
  - SCC-Vision v2: threshold-based on k_sccv × FRIC × g, p97 predicted lateral accel
  - SCC-Map v2: physics cross-validation of mapd speed targets; corner unwind via curvatureRates
  - Speed governor: min(v_cruise, v_scc_map, v_scc_vision, v_sla, v_road_cap, v_weather_cap)
  - Following v2 state machine: CRUISE/GAP_ACQUIRING/FOLLOWING/DECELERATING/STOPPING/STOPPED/REACCEL
  - Jerk filter: 0.5 m/s³ normal, 3.0 m/s³ safety, per-tier override from following controller
  - Weather speed cap: activates when FRIC < 0.6 (wet road detection)
  - All constants externalized to Params["LongV2Tuning"] JSON (no recompile needed)
* SCC badge UI redesign:
  - Green (inactive) → Orange (gas gating) → Red (braking): 300ms smooth color transition
  - When governing, badge shows speed in MPH instead of label
  - Drop shadow, auto-width pill shape
* Terminal server (port 8888):
  - PTY-backed bash shell accessible from any browser at http://<device-ip>:8888
  - Flash & Reboot modal: lists all funnypilot branches sorted by last commit date
  - Auto-reconnects after reboot

FunnyPilot v0.9.8 (2026-03-01)
========================
* Dynamic Speed Limit Assist (Locked Mode):
  - SLA now stays permanently locked once activated (only clears on cruise disengage)
  - When user adjusts cruise while SLA is active, records the offset % relative to limit
    (e.g. 36mph in a 30mph zone = +20% offset)
  - On entering a new speed limit zone, the stored offset is automatically applied
    (e.g. +20% offset + 40mph zone = 48mph effective target)
  - Offset is continuously updated whenever the user adjusts cruise
  - Offset is capped at ±50% for safety
  - Works for automatic speed reductions (SLA slows car to new_limit * (1 + offset))
  - For increases: SLA sets target above current cruise; user confirms with + press
* Dynamic SLA status badge in onroad UI:
  - Teal "SLA" badge appears near the speed limit sign when locked
  - Shows current dynamic offset (e.g. "+20%", "-5%", "±0%")
* Revised follow distance (all distances recalibrated):
  - Distance 2 (standard): 2.5s@≤20mph, gradient 20-35mph, 1.5s@35-50mph, gradient 50-75mph, 1.0s@≥75mph
  - Distance 1 (aggressive): 15% shorter than standard at all speeds
  - Distance 3 (relaxed): 15% longer than standard at all speeds

FunnyPilot v0.9.7h (2026-02-25) HOTFIX
========================
* Fixed plannerd crash on long control enable: empty modelV2 arrays caused ValueError in
  SCC-V gas gating (np.amax/np.percentile on zero-size array); added length guard before
  all numpy reductions in vision_controller.py
* Fixed latcontrol_torque.py: moved `import time` out of hot update() loop to module level

FunnyPilot v0.9.7 (2026-02-25)
========================
* Rebased on sunnypilot v2026.001.000 (2026-02-24)
* Gas gating UI indicator: SCC-V and SCC-M badges turn orange with "GAS GATE" label when active
* Variable follow distance (speed-dependent): closer at highway speeds, more buffer in town
  - Distance 1 (aggressive): 2.5s@<20mph, 1.2s@45mph, 0.75s@75mph+
  - Distance 2 (standard): 3.0s@<20mph, 1.6s@45mph, 1.0s@75mph+
  - Distance 3 (relaxed): 3.8s@<20mph, 2.2s@45mph, 1.4s@75mph+
* Follow distance switching gas gates instead of braking (4s gas gate on distance increase)
* High speed warning now a silent static banner (no audio, no disengage, no NO_ENTRY block)
* Speed limit assist now auto-tracks zone changes when active (no confirmation needed)
  - Manual cruise speed change deactivates SLA until next speed limit zone change
  - Re-prompts for confirmation after each manual override, then auto-tracks again
* New longitudinal tuning:
  - COMFORT_BRAKE reduced 2.5->2.0 m/s² (earlier, gentler braking)
  - STOP_DISTANCE increased 6.0->8.5m (more buffer at stops)
  - Raw aLeadK preserved for fast stoplight reaction (not smoothed)
  - Smoothed dRel and vLeadK for stable tracking
* Lane change torque ramp: 3.5s ramp, starting at 40% (carried from v0.9.6h)
* Smooth stopping: linear torque reduction below 15mph (carried from v0.9.6)
* Extended driver monitoring timeouts: 3x original values (carried from v0.9.6)
* Max acceleration capped at 70% of openpilot defaults (carried from v0.9.6)

sunnypilot Version 2026.001.000 (2026-03-xx)
========================
* What's Changed (sunnypilot/sunnypilot)
  * Complete rewrite of the user interface from Qt C++ to Raylib Python
  * comma four support
  * ui: sunnypilot toggle style by @nayan8teen
  * ui: fix scroll panel mouse wheel behavior by @nayan8teen
  * ui: sunnypilot panels by @nayan8teen
  * sunnylink: centralize key pair handling in sunnylink registration by @devtekve
  * ui: reimplement sunnypilot branding with Raylib by @sunnyhaibin
  * ui: Platform Selector by @Discountchubbs
  * ui: vehicle brand settings by @Discountchubbs
  * ui: sunnylink client-side implementation by @nayan8teen
  * ui: `NetworkUISP` by @Discountchubbs
  * ui: add sunnypilot font by @nayan8teen
  * ui: sunnypilot sponsor tier color mapping by @sunnyhaibin
  * ui: sunnylink panel by @nayan8teen
  * ui: Models panel by @Discountchubbs
  * ui: software panel by @Discountchubbs
  * modeld_v2: support planplus outputs by @Discountchubbs
  * ui: OSM panel by @Discountchubbs
  * ui: Developer panel extension by @Discountchubbs
  * sunnylink: Vehicle Selector support by @sunnyhaibin
  * [TIZI/TICI] ui: Developer Metrics by @rav4kumar
  * [comma 4] ui: sunnylink panel by @nayan8teen
  * ui: lateral-only and longitudinal-only UI statuses support by @royjr
  * sunnylink: elliptic curve keys support and improve key path handling by @nayan8teen
  * sunnylink: block remote modification of SSH key parameters by @zikeji
  * [TIZI/TICI] ui: rainbow path by @rav4kumar
  * [TIZI/TICI] ui: chevron metrics by @rav4kumar
  * ui: include MADS enabled state to `engaged` check by @sunnyhaibin
  * Toyota: Enforce Factory Longitudinal Control by @sunnyhaibin
  * ui: fix malformed dongle ID display on the PC if dongleID is not set by @dzid26
  * SL: Re enable and validate ingestion of swaglogs by @devtekve
  * modeld_v2: planplus model tuning by @Discountchubbs
  * ui: fix Always Offroad button visibility by @nayan8teen
  * Reimplement sunnypilot Terms of Service & sunnylink Consent Screens by @sunnyhaibin
  * [TIZI/TICI] ui: update dmoji position and Developer UI adjustments by @rav4kumar
  * modeld: configurable camera offset by @Discountchubbs
  * [TIZI/TICI] ui: sunnylink status on sidebar by @Copilot
  * ui: Global Brightness Override by @nayan8teen
  * ui: Customizable Interactive Timeout by @sunnyhaibin
  * sunnylink: add units to param metadata by @nayan8teen
  * ui: Customizable Onroad Brightness by @sunnyhaibin
  * [TIZI/TICI] ui: Steering panel by @nayan8teen
  * [TIZI/TICI] ui: Rocket Fuel by @rav4kumar
  * [TIZI/TICI] ui: MICI style turn signals by @rav4kumar
  * [TIZI/TICI] ui: MICI style blindspot indicators by @sunnyhaibin
  * [MICI] ui: display blindspot indicators when available by @rav4kumar
  * [TIZI/TICI] ui: Road Name by @rav4kumar
  * [TIZI/TICI] ui: Blue "Exit Always Offroad" button by @dzid26
  * [TIZI/TICI] ui: Speed Limit by @rav4kumar
  * Reapply "latcontrol_torque: lower kp and lower friction threshold (commaai/openpilot#36619)" by @sunnyhaibin
  * [TIZI/TICI] ui: steering arc by @royjr
  * [TIZI/TICI] ui: Smart Cruise Control elements by @sunnyhaibin
  * [TIZI/TICI] ui: Green Light and Lead Departure elements by @sunnyhaibin
  * [TIZI/TICI] ui: standstill timer by @sunnyhaibin
  * [MICI] ui: driving models selector by @Discountchubbs
  * [TIZI/TICI] ui: Hide vEgo and True vEgo by @sunnyhaibin
  * [TIZI/TICI] ui: Visuals panel by @nayan8teen
  * Device: Retain QuickBoot state after op switch by @nayan8teen
  * [TIZI/TICI] ui: Trips panel by @sunnyhaibin
  * [TIZI/TICI] ui: dynamic ICBM status by @sunnyhaibin
  * [TIZI/TICI] ui: Cruise panel by @sunnyhaibin
  * ui: better wake mode support by @nayan8teen
  * Pause Lateral Control with Blinker: Post-Blinker Delay by @CHaucke89
  * SCC-V: Use p97 for predicted lateral accel by @yasu-oh
  * Controls: Support for Torque Lateral Control v0 Tune by @sunnyhaibin
* What's Changed (sunnypilot/opendbc)
  * Honda: DBC for Accord 9th Generation by @mvl-boston
  * FCA: update tire stiffness values for `RAM_HD` by @dparring
  * Honda: Nidec hybrid baseline brake support by @mvl-boston
  * Subaru Global Gen2: bump steering limits and update tuning by @sunnyhaibin
  * Toyota: Enforce Stock Longitudinal Control by @rav4kumar
  * Nissan: use MADS enabled status for LKAS HUD logic by @downquark7
  * Reapply "Lateral: lower friction threshold (#2915)" (#378) by @sunnyhaibin
  * HKG: add KIA_FORTE_2019_NON_SCC fingerprint by @royjr
  * Nissan: Parse cruise control buttons by @downquark7
  * Rivian: Add stalk down ACC behavior to match stock Rivian by @lukasloetkolben
  * Tesla: remove `TESLA_MODEL_X` from `dashcamOnly` by @ssysm
  * Hyundai Longitudinal: refactor tuning by @Discountchubbs
  * Tesla: add fingerprint for Model 3 Performance HW4 by @sunnyhaibin
  * Toyota: do not disable radar when smartDSU or CAN Filter detected by @sunnyhaibin
  * Honda: add missing `GasInterceptor` messages to Taiwan Odyssey DBC by @mvl-boston
  * GM: remove `CHEVROLET_EQUINOX_NON_ACC_3RD_GEN` from `dashcamOnly` by @sunnyhaibin
  * GM: remove `CHEVROLET_BOLT_NON_ACC_2ND_GEN` from `dashcamOnly` by @sunnyhaibin
* New Contributors (sunnypilot/sunnypilot)
  * @TheSecurityDev made their first contribution in "ui: fix sidebar scroll in UI screenshots"
  * @zikeji made their first contribution in "sunnylink: block remote modification of SSH key parameters"
  * @Candy0707 made their first contribution in "[TIZI/TICI] ui: Fix misaligned turn signals and blindspot indicators with sidebar"
  * @CHaucke89 made their first contribution in "Pause Lateral Control with Blinker: Post-Blinker Delay"
  * @yasu-oh made their first contribution in "SCC-V: Use p97 for predicted lateral accel"
* New Contributors (sunnypilot/opendbc)
  * @AmyJeanes made their first contribution in "Tesla: Fix stock LKAS being blocked when MADS is enabled"
  * @mvl-boston made their first contribution in "Honda: Update Clarity brake to renamed DBC message name"
  * @dzid26 made their first contribution in "Tesla: Parse speed limit from CAN"
  * @firestar5683 made their first contribution in "GM: Non-ACC platforms with steering only support"
  * @downquark7 made their first contribution in "Nissan: use MADS enabled status for LKAS HUD logic"
  * @royjr made their first contribution in "HKG: add KIA_FORTE_2019_NON_SCC fingerprint"
  * @ssysm made their first contribution in "Tesla: remove `TESLA_MODEL_X` from `dashcamOnly`"
* Full Changelog: https://github.com/sunnypilot/sunnypilot/compare/v2025.002.000...v2026.001.000

sunnypilot Version 2025.002.000 (2025-11-06)
========================
* What's Changed (sunnypilot/sunnypilot)
  * models: bump model json to v8 by @Discountchubbs
  * Bug: Model UI Crash Fix by @nayan8teen
  * controlsd: add `CP_SP` to `get_pid_accel_limits` by @THERoenPR
  * sunnylink: update uploader button logic to support novice tier and above by @devtekve
  * Tesla: Coop Steering by @AmyJeanes
  * ui: update discord references and add forum widget by @devtekve
  * ui: Fix spacing in sunnylink panel by @devtekve
  * docs: Update README installation branches and discord links by @mpurnell1 in
  * stats: sunnylink integration by @devtekve
  * bug: Fix initial registration for sunnylink by @devtekve
* What's Changed (sunnypilot/opendbc)
  * Honda: add brake hold messages for Clarity by @mvl-boston
  * interface: add `CP_SP` to `get_pid_accel_limits` method signature by @roenthomas
  * Honda: use fixed accel min/max constants for Gas Interceptor by @roenthomas
  * Tesla: Coop Steering by @AmyJeanes
* New Contributors (sunnypilot/sunnypilot)
  * @THERoenPR made their first contribution in "controlsd: add `CP_SP` to `get_pid_accel_limits`"
  * @AmyJeanes made their first contribution in "Tesla: Coop Steering"
  * @mpurnell1 made their first contribution in "docs: Update README installation branches and discord links"
* Full Changelog: https://github.com/sunnypilot/sunnypilot/compare/v2025.001.000...v2025.002.000

sunnypilot Version 2025.001.000 (2025-10-25)
========================
* 🛠️ Major rewrite
  * Most features are intended to be identical to previous versions with slight improvements
  * Fully adopts upstream commaai’s openpilot, opendbc (car interface and safety), and panda test suites to ensure consistent safety compliance and reliability across all systems
  * Added regression testing to verify expected behavior and maintain stability across core modules
  * Aligns with comma.ai’s safety policy: preserving driver monitoring, actuation checks, and safety test suite coverage
  * Some features have not yet been reimplemented in this rewrite and are temporarily disabled in this release. They may return in future releases once fully ported and validated. See the end of the changelog to get a list of what's not going to be present.
* 🌟 Major Features & Systems
  * Modular Assistive Driving System (MADS)
    * Complete driving assistance framework
  * Driving Model Manager
    * Custom driving model selection with support for about 86 models (as of writing), from Night Strike (October 2023) up to The Cool People’s Models (October 2025)
  * Neural Network Lateral Control (NNLC) (Formerly NNFF)
    * Advanced torque-based lateral control
  * Dynamic Experimental Control (DEC)
    * Intelligent longitudinal control adaptation
  * Speed Limit Assist (SLA)
    * Comprehensive speed limit integration featuring @pfeiferj's `mapd` for offline map limits downloads, a Speed Limit Resolver for sourcing data (from car, map, combined, etc), on-screen UI for Speed Limit Information/Warning, and Speed Limit Assist (SLA) to adjust cruise speed automatically.
    * Currently disabled for Tesla with sunnypilot Longitudinal Control in release and Rivian with sunnypilot Longitudinal Control in all branches
      * May return in future releases
  * Intelligent Cruise Button Management (ICBM)
    * System designed to manage the vehicle’s speed by sending cruise control button commands to the car’s ECU.
  * Smart Cruise Control Map & Vision (SCC-M / SCC-V)
    * When using any form of long control (sunnypilot longitudinal control or ICBM) it will control the speed at which you enter and perform a turn by leveraging map data (SCC-M) and/or by leveraging what the model sees about the curve ahead (SCC-V)
  * Vehicle Selector
    * If your vehicle isn’t fingerprinted automatically, you can still use the vehicle selector to get it working
  * sunnylink Integration
    * Cloud connectivity and settings backup/restore
    * PENDING: The infrastructure is ready for remote setting management, including remote driving model switching. An announcement will be made when this is ready to use in current and future releases.
  * External Storage Support
    * Expanded storage options
  * mapd Integration (thanks to @pfeiferj)
    * Allow downloading OpenStreetMap databases for your area, which could be useful for Speed Limit Assist (SLA)
* User Interface Enhancements
  * Complete UI Redesign from Default openpilot Experience
    * A total overhaul of the sunnypilot offroad user interface for a modern and intuitive experience.
  * New Settings Panels
    * Reorganized settings into dedicated panels: Steering, Longitudinal, Vehicle, Models, Visuals, Display, and Trips.
  * Advanced Controls Toggle
    * Out of the box experience has a slightly reduced set of settings for a lower barrier of entry, once you are ready, you can get a few extra settings by toggling on the Advanced Controls.
  * Models Panel
    * A dedicated panel for model management, featuring a download manager, model folders, a favorites system, fuzzy search, and a cache refresh button.
  * Visuals & Display
    * Extensive customization options including brightness controls, custom interactivity timeouts, green light indicator, lead vehicle indicator, on-screen turn signals, blind spot indicators, lead chevron info, standstill timer, road name display, and a Tesla-like 🌈 rainbow road path.
  * Screen Off while driving
    * Options to turn the screen off while driving and customize wake-up behavior for alerts.
  * Branch & Platform Selectors
    * Improved software management with a searchable branch selector and a platform selector that displays the current fingerprint.
  * Developer UI
    * An enhanced developer UI with better alert positioning and an integrated error log viewer.
  * Convenience Features
    * Added an “Exit Offroad” button, “Always Offroad” mode, Quiet Mode, and customizable max time offroad settings.
  * OpenStreetMap Database Downloader
    * The OpenStreetMap database downloader now includes a search feature for easily finding areas.
* Model and AI Improvements
  * Modular Model Backend
    * Major refactor of `modeld` to support modular runners (SNPE, thneed, tinygrad) and dynamic model inputs.
  * Enhanced Model Outputs
    * Models now provide additional outputs like “turn desires” for improved control.
  * Live Parameter Adjustments
    * Support for live delay adjustments and software delay controls directly from the UI.
  * Model Management
    * Added model caching, automatic refresh capabilities, and shape inference from inputs for better compatibility.
* Control Systems
  * Pause Lateral on Blinker
    * Option to temporarily pause lateral control when the turn signal is active.
  * Custom ACC Setpoint Increments
    * Configure custom increments for adjusting the ACC set speed for applicable vehicle platforms.
  * Steering on Brake Press
    * Customizable steering behavior when the brake pedal is pressed.
  * Enforce Torque Lateral Control
    * New customized settings for fine-tuning torque-based steering.
  * Automatic Lane Change
    * Support for automatic lane changes, including a mode to disable it.
* Technical Infrastructure
  * Custom Cereal Implementation
    * Migrated sunnypilot-specific events, car parameters, and car controls to a dedicated cereal for better compatibility and performance.
  * Car Interface Abstractions
    * Refactored car interfaces to support brand-specific settings and easier integration.
  * Param Store Caching
    * Implemented a cache for the parameter store to reduce startup times, with support for live parameter updates.
  * Enhanced Error Handling
    * Improved exception management and Sentry logging for better stability and debugging.
  * Docker & CI/CD
    * Full Docker image support, a dedicated GitHub runner service, and comprehensive improvements to the entire CI/CD pipeline for automated testing, building, and releasing.
* Bug Fixes and Stability
  * Registration Requirement Removed
    * No longer necessary to register the device to go onroad.
  * Panda Firmware Checks
    * Improved firmware checks to gracefully handle deprecated Panda devices.
  * Numerous Fixes
    * Addressed a wide range of bugs across the system for a more stable and reliable experience.
* Developer Experience
  * CLion IDE integration and external tools
  * Comprehensive testing and build automation
  * Model building and publishing automation
  * UI preview generation and testing
  * Release drafting and version management
  * Code quality and maintenance workflows
* Translations and Localization
  * Korean translation updates
  * Automated translation management system
* ❌ Removed
  * Navigate on openpilot (NoO)
    * Navigate on openpilot (NoO) has been removed as upstream is prioritizing improving the driving model’s capabilities and simplifying the training stack.
    * The feature may return in a future upstream release by comma.ai once model improvements from upstream make it more reliable.
  * Visuals: Rocket Fuel
  * Visuals: Displaying Braking Status
  * Vehicle: Toyota - Enforce Stock Longitudinal Control
  * Subaru: Increase Steering Torque
  * Longitudinal: Acceleration Personality
  * UI: Display CPU Temperature on Sidebar
  * Lateral: Block Lane Change with Road Edge Detection
  * UI: Display DM Camera in Reverse Gear
  * UI: Auto-hide Selected UI Elements
  * Visuals: Display End-to-End Longitudinal Status
  * Toyota: Stop and Go Hack (alpha)
  * Visuals: Onroad Settings
  * Honda: Serial Steering Support
  * Volkswagen: Non-ACC Platforms Support
  * Longitudinal: Dynamic Personality
  * Honda Nidec: Allow Stock Longitudinal Control
  * Lateral Planner: Dynamic Lane Profile
  * Lateral Planner: Laneful Mode
  * Lateral: Custom Camera and Path Offsets
  * Toyota: Door Controls
* New Contributors (sunnypilot/sunnypilot)
  * @royjr made their first contribution in "NNLC: bump max similarity for higher accuracy (#704)"
  * @nayan8teen made their first contribution in "UI: Update AbstractControlSP_SELECTOR and OptionControlSP (#800)"
  * @wtogami made their first contribution in "TOYOTA_RAV4_PRIME NNLC tuning gen 1 (#850)"
  * @dparring made their first contribution in "FCA: Ram 1500 improvements (#797)"
  * @Kirito3481 made their first contribution in "Update ko-kr translation (#1167)"
  * @michael-was-taken made their first contribution in "Reorder README tables: show -new branches first (#1191)"
  * @dzid26 made their first contribution in "params: Fix loading delay on startup (#1297)"
  * @HazZelnutz made their first contribution in "Visuals: Turn signals on screen when blinker is used (#1291)"
  * @sirmuskrat made their first contribution in "ui: openpilot Longitudinal Control → sunnypilot Longitudinal Control (#1422)"
* New Contributors (sunnypilot/opendbc)
  * @chrispypatt made their first contribution in "Toyota: SecOC Longitudinal Control (sunnypilot/opendbc#93)"
  * @Discountchubbs made their first contribution in "Hyundai: EPS FW For 2022 KIA_NIRO_EV SCC (sunnypilot/opendbc#118)"
  * @lukasloetkolben made their first contribution in "Tesla: enableBsm is always true (sunnypilot/opendbc#163)"
  * @roenthomas made their first contribution in "Honda: int flag for modified EPS configs (sunnypilot/opendbc#254)"
  * @AmyJeanes made their first contribution in "Tesla: Fix stock LKAS being blocked when MADS is enabled (sunnypilot/opendbc#286)"
  * @mvl-boston made their first contribution in "Honda: Update Clarity brake to renamed DBC message name (sunnypilot/opendbc#282)"
  * @dzid26 made their first contribution in "Tesla: Parse speed limit from CAN (sunnypilot/opendbc#308)"
  * @firestar5683 made their first contribution in "GM: Non-ACC platforms with steering only support (sunnypilot/opendbc#229)"
************************
* Synced with commaai's openpilot (v0.10.1)
  * master commit c9dbf97649a27117be6d5955a49e2d4253337288 (September 12, 2025)
* New driving model
  * World Model: removed global localization inputs
  * World Model: 2x the number of parameters
  * World Model: trained on 4x the number of segments
  * Driving Vision Model: trained on 4x the number of segments
* Honda City 2023 support thanks to vanillagorillaa and drFritz!
* Honda N-Box 2018 support thanks to miettal!
* Honda Odyssey 2021-25 support thanks to csouers and MVL!

sunnypilot - 0.9.7.1 (2024-06-13)
========================
* New driving model
  * Inputs the past curvature for smoother and more accurate lateral control
  * Simplified neural network architecture in the model's last layers
  * Minor fixes to desire augmentation and weight decay
* New driver monitoring model
  * Improved end-to-end bit for phone detection
* Adjust driving personality with the follow distance button
* Support for hybrid variants of supported Ford models
* Fingerprinting without the OBD-II port on all cars
* Improved fuzzy fingerprinting for Ford and Volkswagen
************************
* UPDATED: Synced with commaai's openpilot
  * master commit f8cb04e (June 10, 2024)
* NEW❗: sunnylink (Alpha early access)
  * NEW❗: Config/Settings Backup
    * Remotely back up and restore sunnypilot settings easily
    * Device registration with sunnylink ensures a secure, integrated experience across services
    * AES encryption derived from the device's RSA private key is used for utmost security
    * Settings are encrypted on-device, transmitted securely via HTTPS, and stored encrypted on sunnylink
    * Prevents loss of settings after device resets, offering peace of mind through end-to-end encryption
    * Early alpha access to all current and previous GitHub Sponsors and Patreon supporters
  * GitHub account pairing from device settings scanning QR code
    * Pairing your account will allow you to access features via our API (still WIP but accessible if you dig a little on our code 😉)
    * Allow inheritance of your sponsorship status, allowing you to get extra features and early access whenever applicable
* NEW❗: iOS Siri Shortcuts Navigation support thanks to twilsonco and mike86437!
  * iOS and macOS Shortcuts to quickly set navigation destinations from your iOS device
  * comma Prime support
  * Personal Mapbox/Amap/Google Maps token support
  * Instructions on how to set up your iOS Siri Shortcuts: https://routinehub.co/shortcut/17677/
* NEW❗: Forced Offroad mode
  * Force sunnypilot in the offroad state even when the car is on
  * When Forced Offroad mode is on, allows changing offroad-only settings even when the car is turned on
  * To engage/disengage Force Offroad, go to Settings -> Device panel
* UPDATED: Auto Lane Change Timer -> Auto Lane Change by Blinker
  * NEW❗: New "Off" option to disable lane change by blinker
* UPDATED: Pause Lateral Below Speed with Blinker
  * NEW❗: Customizable Pause Lateral Speed
    * Pause lateral actuation with blinker when traveling below the desired speed selected. Default is 20 MPH or 32 km/h.
* UPDATED: Hyundai CAN Longitudinal
  * Auto-enable radar tracks on platforms with applicable Mando radar
* UPDATED: Hyundai CAN-FD Camera-based SCC
  * NEW❗: Parse lead info for camera-based SCC platforms with longitudinal support
    * Improve lead tracking when using openpilot longitudinal
* RE-ENABLED: Map-based Turn Speed Control (M-TSC) for supported platforms
  * openpilot Longitudinal Control available cars
  * Custom Stock Longitudinal Control available cars
* UPDATED: Continued support for comma Pedal
  * In response to the official deprecation of support for comma Pedal in the upstream, sunnypilot will continue maintaining software support for comma Pedal
* UPDATED: Driving Model Selector v4
  * NEW❗: Driving Model additions
    * North Dakota (April 29, 2024) - NDv2
    * WD40 (April 09, 2024) - WD40
    * Duck Amigo (March 18, 2024) - DA
    * Recertified Herbalist (March 01, 2024) - CHLR
  * Legacy Driving Models with Navigate on openpilot (NoO) support
    * Includes Duck Amigo and all preceding models
* UPDATED: Bumping mapd by [@pfeiferj](https://github.com/pfeiferj) to version [v1.9.0](https://github.com/pfeiferj/mapd/releases/tag/v1.9.0) thanks to pfeiferj!
* UPDATED: Reset Mapbox Access Token -> Reset Access Tokens for Map Services
  * Reset self-service access tokens for Mapbox, Amap, and Google Maps
* UPDATED: Upstream native support for Gap Adjust Cruise
* UPDATED: Neural Network Lateral Control (NNLC)
  * Due to upstream changes with platform simplifications, most platforms will match and fallback to combined platform model
  * This will be updated when the new mapping of platforms are restructured (thanks @twilsonco 😉)
* UI Updates
  * Display Metrics Below Chevron
    * NEW❗: Metrics is now being displayed below the chevron instead of above
    * NEW❗: Display both Distance and Speed simultaneously
    * NEW❗: View sunnylink connectivity status on the left sidebar!

sunnypilot - 0.9.6.2 (2024-05-29)
========================
* REMOVED: Screen Recorder
  * Screen Recorder is removed due to unnecessary resource usage
  * An improved version will be available in the near future. Stay tuned!

sunnypilot - 0.9.6.1 (2024-02-27)
========================
* New driving model
  * Vision model trained on more data
  * Improved driving performance
  * Directly outputs curvature for lateral control
* New driver monitoring model
  * Trained on larger dataset
* AGNOS 9
* comma body streaming and controls over WebRTC
* Improved fuzzy fingerprinting for many makes and models
* Alpha longitudinal support for new Toyota models
* Chevrolet Equinox 2019-22 support thanks to JasonJShuler and nworb-cire!
* Dodge Durango 2020-21 support
* Hyundai Staria 2023 support thanks to sunnyhaibin!
* Kia Niro Plug-in Hybrid 2022 support thanks to sunnyhaibin!
* Lexus LC 2024 support thanks to nelsonjchen!
* Toyota RAV4 2023-24 support
* Toyota RAV4 Hybrid 2023-24 support
************************
* UPDATED: Synced with commaai's openpilot
  * master commit db57a21 (February 22, 2024)
  * v0.9.6 release (February 27, 2024)
* UPDATED: Dynamic Experimental Control (DEC)
  * Synced with dragonpilot-community/dragonpilot:beta3 commit f4ee52f
* NEW❗: Default Driving Model: Certified Herbalist v2 (February 13, 2024)
* UPDATED: Driving Model Selector v3
  * NEW❗: Driving Model additions
    * Certified Herbalist v2 (February 13, 2024) - CHv2
    * Certified Herbalist (February 5, 2024) - CH
    * Los Angeles v2 (January 24, 2024) - LAv2
    * Los Angeles (January 22, 2024) - LAv1
  * NEW❗: Model Caching thanks to DevTekVE!
    * Model caching allows the selection of previously downloaded Driving Model
    * Users can now access cached versions of selected models, eliminating redundant downloads for previously fetched models
  * Legacy Driving Models support
    * New Delhi (December 21, 2023) - ND
    * Blue Diamond v2 (December 11, 2023) - BDv2
    * Blue Diamond (November 18, 2023) - BDv1
    * Farmville (November 7, 2023) - FV
    * Night Strike (October 3, 2023) - NS
  * Certain features are deprecated with newer Driving Models
    * Dynamic Lane Profile (DLP)
    * Custom Offsets
* UPDATED: Dynamic Lane Profile (DLP)
  * Continued support for Legacy Driving Models (e.g., ND, BDv2, BDv1, FV, NS)
  * Deprecated support for newer Driving Models (e.g., CHv2, CH, LAv2, LAv1)
* UPDATED: Custom Offsets
  * Continued support for Legacy Driving Models (e.g., ND, BDv2, BDv1, FV, NS)
  * Deprecated support for newer Driving Models (e.g., CHv2, CH, LAv2, LAv1)
* UPDATED: Hyundai/Kia/Genesis - ESCC Radar Interceptor
  * Message parsing improvements with the latest firmware update: https://github.com/sunnypilot/panda/tree/test-escc-smdps
* UI Updates
  * NEW❗: Visuals: Display Feature Status toggle
    * Display the statuses of certain features on the driving screen
  * NEW❗: Visuals: Enable Onroad Settings toggle
    * Display the Onroad Settings button on the driving screen to adjust feature options on the driving screen, without navigating into the settings menu
  * REMOVED: "Device ambient" temperature option on the sidebar
* FIXED: New comma 3X support
* FIXED: New comma eSIM support
* Bug fixes and performance improvements

sunnypilot - 0.9.5.3 (2023-12-24)
========================
* UPDATED: Dynamic Experimental Control (DEC)
  * Synced with dragonpilot-community/dragonpilot:lp-dp-beta2 commit 578d38b
* UPDATED: Driving Model Selector v2
  * Driving models sort in descending order based on availability date
  * Experimental/unmerged driving models are only available in "dev-c3" branch
    * To select and use experimental driving models, navigate to "Software" panel, select the "dev-c3" branch, and check for update
* UPDATED: Vision-based Turn Speed Control (V-TSC) implementation
  * Refactored implementation thanks to pfeiferj!
  * More accurate and consistent velocity calculation to achieve smoother longitudinal control in curves
* NEW❗: Speed Limit Warning
  * Display alert and/or chime to warn the driver when the cruising speed is faster than the speed limit plus the Warning Offset
  * Customizable Warning Offset, independent of Speed Limit Control (SLC)'s Limit Offset
* UPDATED: Speed Limit Source Policy
  * Selectable speed limit source for Speed Limit Control and Speed Limit Warning
  * Applicable to: Speed Limit Control, Speed Limit Warning
* UPDATED: Speed Limit Control (SLC)
  * Engage Mode: Removed "Warning Only" mode - this has been replaced by the new Speed Limit Warning sub-menu
* UPDATED: OpenStreetMap (OSM) implementation
  * Refactored implementation thanks to pfeiferj!
    * Less resource impact
    * Significantly smaller sizes with databases
    * All regions are available to download
    * Weekly map updates thanks to pfeiferj!
    * Increased the font size of the road name
  * C3X-specific changes
    * Altitude (ALT.) display on Developer UI
    * Current street name on top of driving screen when "OSM Debug UI" is enabled
* UPDATED: Map-based Turn Speed Control (M-TSC) implementation
  * Only available in "staging-c3" and "dev-c3" branches. If you are using "release-c3" branch, navigate to "Software" panel, select the desired target branch, and check for update
  * Refactored implementation thanks to pfeiferj!
  * Based on the new OpenStreetMap implementation
  * Improved predicted curvature calculations from OpenStreetMap data
* UI updates
  * RE-ENABLED: Navigation: Full screen support
    * Display the map view in full screen
    * To switch back to driving view, tap on the border edge
* Hyundai Bayon Non-SCC 2019 support thanks to polein78!

sunnypilot - 0.9.5.2 (2023-12-07)
========================
* NEW❗: MADS: Allow Navigate on openpilot in Chill Mode
  * Allow navigation to feed map view into the driving model while using Chill Mode
  * Support all platforms, including platforms that do not support openpilot longitudinal control & Experimental Mode
* NEW❗: Neural Network Lateral Controller
  * Formerly known as "NNFF", this replaces the lateral "torque" controller with one using a neural network trained on each car's (actually, each separate EPS firmware) driving data for increased controls accuracy
  * Contact @twilsonco in the sunnypilot Discord server with feedback, or to provide log data for your car if your car is currently unsupported
* NEW❗: Driving Model Selector
  * Easily switch between driving models without reinstalling branches. Offering immediate access to the latest models upon release
    * An internet connection is required for downloading models. Each model switch currently involves downloading the model again. Future updates may allow for offline switching
  * Warning is displayed for metered connections to avoid unexpected data usage if on cellular data
  * Change driving models via **Settings -> Software -> Current Driving Model**.
* NEW❗: Hyundai CAN longitudinal:
  * NEW❗: Enable radar tracks for certain Santa Fe platforms
    * Internal Combustion Engine (ICE) 2021-23
    * Hybrid 2022-23
    * Plug-in Hybrid 2022-23
* NEW❗: Lane Change: When manually braking with steering engaged, turning on the turn signal will default to Nudge mode
* Volkswagen MQB CC only platforms (radar or no radar) support thanks to jyoung8607!

sunnypilot - 0.9.5.1 (2023-11-17)
========================
* UPDATED: Synced with commaai's master commit e94c3c5
* NEW❗: Farmville driving model
* NEW❗: Onroad Settings Panel
  * Onroad buttons (i.e., DLP, GAC) moved to its dedicated panel
    * Driving Personality
    * Dynamic Lane Profile (DLP)
    * Dynamic Experimental Control (DEC)
    * Speed Limit Control (SLC)
* NEW❗: Display main feature status on onroad view in real-time
  * GAP - Driving Personality
  * DLP - Dynamic Lane Profile
  * DEC - Dynamic Experimental Control
  * SLC - Speed Limit Control
* NEW❗: Dynamic Experimental Control (DEC) thanks to dragonpilot-community!
  * Automatically determines and selects between openpilot ACC and openpilot End to End longitudinal based on conditions for a more natural drive
  * Dynamic Experimental Control is only active while in Experimental Mode
  * When Dynamic Experimental Control is ON, initially setting cruise speed will set to the vehicle's current speed
* NEW❗: Hyundai CAN longitudinal:
  * NEW❗: Parse lead info for camera-based SCC platforms
    * Improve lead tracking when using openpilot longitudinal
  * NEW❗: Parse lead distance to display on car cluster
    * Introduced better lead distance calculation to display on the car's cluster, replacing the binary "lead visible" indication on the SCC cluster
    * Lead distance is now categorized into different ranges for more detailed and comprehensive information to the driver similar to how stock ACC does it
  * NEW❗: Parse speed limit sign recognition from camera for certain supported platforms
* NEW❗: Subaru - Stop and Go auto-resume support thanks to martinl!
  * Global (excluding Gen 2 and Hybrid) and Pre-Global support
* NEW❗: Toyota - Stop and Go hack
  * Allow some Toyota/Lexus cars to auto resume during stop and go traffic
  * Only applicable to certain models and model years
* NEW❗: Toyota: ZSS support thanks to dragonpilot-community and ErichMoraga!
* NEW❗: MSPA (Cereal structs refactor)
  * Make sunnypilot Parsable Again - @sshane
  * sunnypilot is now parsable with stock openpilot tools
* NEW❗: Display 3D buildings on map thanks to jakethesnake420!
* openpilot Longitudianl Control capable cars only
  * UPDATED: Gap Adjust Cruise is now a part of Driving Personality
    * [DISTANCE/FOLLOW DISTANCE/GAP DISTANCE] physical button on the steering wheel to select Driving Personality on by default
    * Status now viewable in onroad view or Onroad Settings Panel
    * REMOVED: Gap Adjust Cruise toggle
* UPDATED: Speed Limit Control (SLC)
  * NEW❗: Speed Limit Engage Mode
    * Select the desired mode to set the cruising speed to the speed limit
      * Warning Only: Warn the driver when the vehicle is driven faster than the speed limit
      * Auto: Automatic speed adjustment on motorways based on speed limit data
      * User Confirm: Inform the driver to change set speed of Adaptive Cruise Control to help the driver stay within the speed limit
    * Supported platforms
      * openpilot Longitudinal Control available cars (Excluding certain Toyota/Lexus, Ford, explained below)
      * Custom Stock Longitudinal Control available cars
    * Unsupported platforms
      * Toyota/Lexus and Ford - most platforms do not allow us to control the PCM's set speed, requires testers to verify
  * NEW❗: Speed limit source selector
    * Select the desired precedence order of sources used to adapt cruise speed to road limits
* UPDATED: Custom Stock Longitudinal Control
  * RE-ENABLED: Hyundai/Kia/Genesis CAN-FD platforms
* UPDATED: Custom Offsets reimplementation
  * Camera Offset only works in Laneful (Laneful Only or Laneful in Auto mode when using Dynamic Lane Profile)
  * Path Offset can be applied to both Laneless and Laneful
* UPDATED: Refactored Torque Lateral Control custom tuning menu
  * NEW❗: Less Restrict Settings for Self-Tune (Beta)
  * NEW❗: Custom Tuning for setting offline and live values in real-time
* UPDATED: Auto-detect custom Mapbox token if a personal Mapbox token is provided
  * REMOVED: "Enable Mapbox Navigation" toggle
* UI updates
  * New Settings menu redesign and improved interactions
* FIXED: Retain hotspot/tethering state was not consistently saved
* FIXED: Map stuck in "Map Loading" if comma Prime is active
* FIXED: OpenStreetMap implementation on C3X devices
  * M-TSC
  * Altitude (ALT.) display on Developer UI
  * Current street name on top of driving screen when "OSM Debug UI" is enabled
* Hyundai Kona Non-SCC 2019 support thanks to Quex!
* Kia Seltos Non-SCC 2023-24 support thanks to Moodkiller and jeroid_!

sunnypilot - 0.9.4.1 (2023-08-11)
========================
* UPDATED: Synced with commaai's 0.9.4 release
* NEW❗: Moonrise driving model
* NEW❗: Ford upstream models support
* UPDATED: Dynamic Lane Profile selector in the "SP - Controls" menu
* REMOVED: Dynamic Lane Profile driving screen UI button
* FIXED: Disallow torque lateral control for angle control platforms (e.g. Ford, Nissan, Tesla)
  * Torque lateral control cannot be used by angle control platforms, and would cause a "Controls Unresponsive" error if Torque lateral control is enforced in settings
* REMOVED: Speed Limit Style override
* Honda Accord 2016-17 support thanks to mlocoteta!
  * Serial Steering hardware required. For more information, see https://github.com/mlocoteta/serialSteeringHardware
* mapd: utilize advisory speed limit in curves (#142) thanks to pfeiferj!

sunnypilot - 0.9.3.1 (2023-07-09)
========================
* UPDATED: Synced with commaai's 0.9.3 release
* NEW❗: Display Temperature on Sidebar toggle
  * Display Ambient temperature, memory temperature, CPU core with the highest temperature, GPU temperature, or max of Memory/CPU/GPU on the sidebar
  * Replace "Display CPU Temperature on Sidebar" toggle
* NEW❗: Hot Coffee driving model
* NEW❗: HKG CAN: Smoother Stopping Performance (Beta) toggle
  * Smoother stopping behind a stopped car or desired stopping event.
  * This is only applicable to HKG CAN platforms using openpilot longitudinal control
* NEW❗: Toyota: TSS2 longitudinal: Custom Tuning
  * Smoother longitudinal performance for Toyota/Lexus TSS2/LSS2 cars thanks to dragonpilot-community!
* NEW❗: Enable Screen Recorder toggle
  * Enable this will display a button on the onroad screen to toggle on or off real-time screen recording with UI elements.
* IMPROVED: Dynamic Lane Profile: when using Laneline planner via Laneline Mode or Auto Mode, enforce Laneless planner while traveling below 10 MPH or 16 km/h
* REMOVED: Display CPU Temperature on Sidebar

sunnypilot - 0.9.2.3 (2023-06-18)
========================
* NEW❗: Auto Lane Change: Delay with Blind Spot
  * Toggle to enable a delay timer for seamless lane changes when blind spot monitoring (BSM) detects an obstructing vehicle, ensuring safe maneuvering
* NEW❗: Driving Screen Off: Wake with Non-Critical Events
  * When Driving Screen Off Timer is not set to "Always On":
    * Enabled: Wake the brightness of the screen to display all events
    * Disabled: Wake the brightness of the screen to display critical events
  * Currently, all non-nudge modes are default to continue lane change after 1 seconds of blind spot detection
* NEW❗: Fleet Manager PIN Requirement toggle
  * User can now enable or disable PIN requirement on the comma device before accessing Fleet Manager
* NEW❗: Reset all sunnypilot settings toggle
* NEW❗: Turn signals display on screen when blinker is used
  * Green: Blinker is on
  * Red: Blinker is on, car detected in the adjacent blind spot or road edge detected
* IMPROVED: mapd: better exceptions handling when loading dependencies
* UPDATED: Green Traffic Light Chime no longer displays an orange border when executed
* FIXED: mapd: Road name flashing caused by desync with last GPS timestamp
* FIXED: Ram HD (2500/3500): Ignore paramsd sanity check
  * Live parameters have trouble with self-tuning on this platform with upstream openpilot 0.9.2
* Hyundai: Longitudinal support for CAN-based Camera SCC cars thanks to Zack1010OP's Patreon sponsor!

sunnypilot - 0.9.2.2 (2023-06-13)
========================
* NEW❗: Toyota: Allow M.A.D.S. toggling with LKAS Button (Beta)
* IMPROVED: Ram: cruise button handling

sunnypilot - 0.9.2.1 (2023-06-10)
========================
* UPDATED: Synced with commaai's 0.9.2 release
* UPDATED: feature revamp with better stability
* UPDATED:
  * M.A.D.S.
    * Path color becomes LIGHT ORANGE during Driver Steering Override
  * Gap Adjust Cruise (now known as Driving Personality in upstream openpilot 0.9.3):
    * Updated profiles and jerk changes
    * Experimental Mode support
    * Three settings: Stock, Aggressive, and Maniac
    * Stock is recommended and the default
    * In Aggressive/Maniac mode, lead follow distance is shorter and quicker gas/brake response
  * Dynamic Lane Profile
    * Display blue borders on both sides of the driving path when Laneline mode is being used in the planner
    * Auto Mode optimization
      * Permanent: Laneless during Auto Lane Change execution
  * Mapd
    * OpenStreetMap Database: new regions added
  * Developer UI (Dev UI)
    * REMOVED: 2-column design
    * NEW❗: 1-column + 1-row design
  * Custom Stock Longitudinal Control
    * NEW❗: Chrysler/Jeep/Ram support
    * NEW❗: Mazda support
    * NEW❗: Volkswagen PQ support
    * DISABLED: Hyundai/Kia/Genesis CAN-FD platforms
* NEW❗: Switch between Chill (openpilot ACC) and Experimental (E2E longitudinal) with DISTANCE button on the steering wheel
  * To switch between Chill and Experimental Mode: press and hold the DISTANCE button on the steering wheel for over 0.5 second
  * All openpilot longitudinal capable cars support
* NEW❗: Nicki Minaj driving model
* NEW❗: Nissan and Mazda upstream models support
* NEW❗: Pre-Global Subaru upstream models support
* NEW❗: Display End-to-end Longitudinal Status (Beta)
  * Display an icon that appears when the End-to-end model decides to start or stop
* NEW❗: Green Traffic Light Chime (Beta)
  * A chime will play when the traffic light you are waiting for turns green, and you have no vehicle in front of you.
* NEW❗: Lead Vehicle Departure Alert
  * Notify when the leading vehicle drives away
* NEW❗: Speedometer: Display True Speed
  * Display the true vehicle current speed from wheel speed sensors.
* NEW❗: Speedometer: Hide from Onroad Screen
* NEW❗: Auto-Hide UI Buttons
  * Hide UI buttons on driving screen after a 30-second timeout. Tap on the screen at anytime to reveal the UI buttons
  * Applicable to Dynamic Lane Profile (DLP) and Gap Adjust Cruise (GAC)
* NEW❗: Display DM Camera in Reverse Gear
  * Show Driver Monitoring camera while the car is in reverse gear
* NEW❗: Block Lane Change: Road Edge Detection (Beta)
  * Block lane change when road edge is detected on the stalk actuated side
* NEW❗: Display CPU Temperature on Sidebar
  * Display the CPU core with the highest temperature on the sidebar
* NEW❗: Display current driving model in Software settings
* NEW❗: HKG: smartMDPS automatic detection (installed with applicable firmware)
* FIXED: Unintended siren/alarm from the comma device if the vehicle is turned off too quickly in PARK gear
* FIXED: mapd: Exception handling for loading dependencies
* Fleet Manager via Browser support thanks to actuallylemoncurd, AlexandreSato, ntegan1, and royjr!
  * Access your dashcam footage, screen recordings, and error logs when the car is turned off
  * Connect to the device via Wi-Fi, mobile hotspot, or tethering on the comma device, then navigate to http://ipAddress:5050 to access.
* Honda Clarity 2018-22 support thanks to mcallbosco, vanillagorillaa and wirelessnet2!
* Ram: Steer to 0/7 MPH support thanks to vincentw56!
* Retain hotspot/tethering state across reboots thanks to rogerioaguas!

sunnypilot - Version Latest (2023-02-22)
========================
* UPDATED: Synced with commaai's master branch - 2023.02.19-04:52:00:GMT - 0.9.2
* Refactor sunnypilot features to be more stable

sunnypilot - Version Latest (2022-12-16)
========================
* UPDATED: Synced with commaai's master branch - 2022.12.16-06:31:00:GMT - 0.9.1
* NEW❗: GM:
    * NEW❗: Gap Adjust Cruise support - Chill, Normal, Aggressive
    * NEW❗: Experimental Mode: Hold DISTANCE button on the steering wheel for 0.5 second to switch between Experimental Mode and Chill Mode
* REMOVED❌: Toytoa: SnG Hack
    * This method is not recommended and may cause some cars to not behave as expected
    * SDSU is strongly recommended to enable SnG for Toyota vehicles without SnG from factory
* commaai: radard: add missing accel data for vision-only leads (commaai/openpilot#26619) - pending PR
    * VOACC performance is drastically improved when using Chill Mode
* IMPROVED: M.A.D.S. events handling
* IMPROVED: UI: screen recorder button change
* IMPROVED: OpenStreetMap Offline Database optimization
* FIXED: Toyota: vehicles' LKAS button no longer has a delay with toggling M.A.D.S.
* FIXED: Toyota: brake pedal press at standstill causing Cruise Fault
* FIXED: Volkswagen MQB: reduce Camera Malfunction occurrences (requires testing)
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-12-10)
========================
* IMPROVED: NEW❗ Developer UI design
    * Second column metrics is now moved to the bottom of the screen
        * ACC. = Acceleration
        * L.S. = Lead Speed
        * E.T. = EPS Torque
        * B.D. = Bearing Degree
        * FRI. = Friction
        * L.A. = Lateral Acceleration
        * ALT. = Altitude
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-12-07)
========================
* NEW❗: Screen Recorder support thanks to neokii and Kumar!
* NEW❗: End-to-end longitudinal start/stop status icon
    * Only appears when Experimental Mode is enabled
* NEW❗: End-to-end longitudinal car chime when starting
    * Hyundai/Kia/Genesis CAN platform, Honda/Acura Bosch/Nidec, Toyota/Lexus
    * i.e. Traffic light turns green, stop sign ready to go, etc.
    * Only appears when Experimental Mode is enabled AND longitudinal control is disengaged
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-12-05)
========================
* UPDATED: Synced with commaai's master branch - 2022.12.04-22:46:00:GMT - 0.9.1
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-11-12)
========================
* UPDATED: Synced with commaai's master branch - 2022.11.12-10:02:00:GMT - 0.8.17
* FIXED: CAN Error for CAN HKG cars that do not have navigation from the factory
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-11-11)
========================
* UPDATED: Synced with commaai's master branch - 2022.11.11-21:22:00:GMT - 0.8.17
* commaai: AGNOS 6.2 (commaai/openpilot#26441)
* NEW❗: Speed Limit Control - HKG - add speed limit from car's navigation head unit
    * Compatible with certain models, trims, and model years
* DISABLED: FCA: RAM HD - steer down to 0
* FIXED: UI: End-to-end longitudinal button on driving screen synchronization
* FIXED: Honda: Longitudinal status with set cruise speed now displays properly in the car's dashboard
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-11-08)
========================
* ADDED: New Zealand offline OpenStreetMap database

sunnypilot - Version Latest (2022-11-04)
========================
* UPDATED: Synced with commaai's master branch - 2022.11.05-01:44:00:GMT - 0.8.17
* RE-ENABLED: Dynamic Lane Profile - preserves lanelines
    * Can be found in "SP - Controls" menu
* NEW❗: DLP: switch to laneless for current/future curves thanks to @twilsonco!
    * Can be found in "SP - Controls" menu
* NEW❗: UI: Road Camera Selector
    * Enable this will display a button on the driving screen to select the driving camera
    * Can be found in "SP - Visuals" menu
* NEW❗: Controls: Camera & Path Custom Offsets
    * Only applicable to laneline mode when using Dynamic Lane Profile
* NEW❗: Buttons on driving screen are now sorted based on priority and availability
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-28)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.28-03:53:00:GMT - 0.8.17
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-26)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.26-06:20:00:GMT - 0.8.17
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-25)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.25-23:53:00:GMT - 0.8.17
* Pre-Global Subaru support thanks to @martinl!
* NEW❗: Speed Limit values turn red when current speed is higher than posted speed limit
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-23)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.22-23:15:00:GMT - 0.8.17
* IMPROVED: Custom Stock Longitudinal Control - HKG - only allow engagement on user button press
* IMPROVED: Custom Stock Longitudinal Control - Volkswagen MQB & PQ - more consistent set speed change
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-21)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.21-17:33:00:GMT - 0.8.17
* IMPROVED: Custom Stock Longitudinal Control - Volkswagen MQB & PQ - more predictable button send logic
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-20)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.20-20:25:00:GMT - 0.8.17
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-19)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.19-08:31:00:GMT - 0.8.17
* IMPROVED: Controls: Speed Limit Control - accelerator press only disengage if "Disengage on Accelerator Pedal" is enabled
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-18)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.18-04:44:00:GMT - 0.8.17
* RE-ENABLED: Volkswagen MQB & PQ with Custom Stock Longitudinal Control
* NEW❗: Steering Rate Cost Live Tune
    * Enables live tune for Steering Rate Cost. Lower value allows steering wheel to move more freely at low speed
    * Can be found in "SP - Controls" menu
* FIXED: MADS: GM - include Regen Paddle logic thanks to @twilsonco!
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-17)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.17-23:54:00:GMT+1 - 0.8.17
* ENABLED: "Custom Stock Longitudinal Control" toggle for CAN-FD cars
* FIXED: HKG CAN-FD: Could not engage when openpilot longitudinal is enabled
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-13)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.13-19:43:00:GMT+1 - 0.8.17
* ADDED: Live Tmux toggle
    * Can be found in "SP - General" menu
* IMPROVED: OpenStreetMap Database Update - only check for database update with explicit user decision
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-11)
========================
* ADDED: Hyundai openpilot longitudinal improvements - huge thanks to @aragon7777!
* ADDED: Check for OpenStreetMap Database Update button
* UPDATED: commaai: Low speed lateral control improvements (commaai:openpilot#26022, bbcd448) - pending PR
* FIXED: MUTCD speed limit spacing adjusts dynamically when no subtext is shown (i.e., speed limit offset, distance to next speed limit)
* FIXED: MADS: Intermittent CAN Error when engaging for Toyota Prius TSS-P
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-09)
========================
* ADDED: commaai: Low speed lateral control improvements (commaai:openpilot#26022, bca288bb) - pending PR
* FIXED: MADS: Intermittent CAN Error when engaging for Toyota Prius TSS-P
* IMPROVED: mapd: stop signs and other supported traffic_calming tags are now slowing/stopping as expected
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-08)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.08-12:07:00:GMT+1 - 0.8.17
* FIXED: MADS: Intermittent CAN Error when engaging for Toyota Prius TSS-P
* IMPROVED: mapd: Speed Humps are now set at 20 MPH or 32 km/h
* IMPROVED: OpenStreetMap Offline Database download experience
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-07)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.07-08:16:00:GMT - 0.8.17
* NEW❗: OpenStreetMap database can now be downloaded locally for offline use
    * Now offering US South, US West, US Northeast, US Florida, Taiwan, and South Africa
    * Databases updated - 2022.10.05-03:30:00:GMT
* NEW❗: mapd: Stop Sign, Yield, Speed Bump, Speed Hump, Sharp Curve support - huge thanks to @move-fast and @dragonpilot-community!
    * Go to https://openstreetmap.org and start mapping out your area!
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-09-30)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.30-22:43:00:GMT - 0.8.17
* RE-ADDED: Torque Lateral Controller Live Tune Menu
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-09-23)
========================
* ADDED: Developer UI: latAccelFactorFiltered & frictionCoefficientFiltered values displays in green if Torque is using live params
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-09-22)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.19-22:19:00:GMT - 0.8.17
* NEW❗: Toggle to explicitly enable Custom Stock Longitudinal Control
    * Applicable cars only: Honda, Hyundai/Kia/Genesis
    * Settings -> Toggles menu

sunnypilot - Version Latest (2022-09-21)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.19-22:19:00:GMT - 0.8.17
* ADDED: Toggle to enable Live Torque (self/auto tune) with Torque lateral controller
    * To enable, first enable "Enforce Torque Lateral Controller" toggle
* UPDATED: New metrics in Developer UI (when Live Torque is enabled)
    * REMOVED: latAccelFactorRaw & frictionCoefficientRaw from torqued
    * ADDED: latAccelFactorFiltered & frictionCoefficientFiltered from torqued
* REMOVED: Temporary remove Torque Lateral Controller Live Tune Menu

sunnypilot - Version Latest (2022-09-20)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.19-22:19:00:GMT - 0.8.17
* ADDED: Toggle to enable Live Torque (self/auto tune) with Torque lateral controller
    * To enable, first enable "Enforce Torque Lateral Controller" toggle
* REMOVED: Temporary remove Torque Lateral Controller Live Tune Menu

sunnypilot - Version Latest (2022-09-18)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.17-11:23:00:GMT - 0.8.17
* ADDED: Kia Forte Non-SCC 2019 support for @askalice
* FIXED: Torque Lateral Control Live Tune now syncs with commaai:openpilot#25822
* FIXED: mapd dependencies no longer need to be re-downloaded after unknown reboots

sunnypilot - Version Latest (2022-09-17)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.17-11:23:00:GMT - 0.8.17
* NEW❗: Non SCC HKG support
    * Custom Stock Longitudinal Control
    * ❗No❗ openpilot longitudinal control
* FIXED: Honda Bosch random low-value set speed changes

sunnypilot - Version Latest (2022-09-16)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.16-20:23:00:GMT - 0.8.17

sunnypilot - Version Latest (2022-09-15)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.16-02:00:00:GMT - 0.8.17
* FIXED: Block additional auto lane change actions if blinker stays on after the first lane change
* REVERTED: Some Toyota with LKAS button no longer requires double press to engage/disengage M.A.D.S.

sunnypilot - Version Latest (2022-09-14)u
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: GM models supported in Force Car Recognition (FCR)
    * Under "SP - Vehicles"
* NEW❗: Prompt to select car in "SP - Vehicles" if car unrecognized on startup
* FIXED: Some Toyota with LKAS button no longer requires double press to engage/disengage M.A.D.S.
* UPDATED: ESCC: Use radar tracks from radar if available

sunnypilot - Version Latest (2022-09-13)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: New metric in Developer UI
    * Actual Lateral Acceleration (Roll Compensated)

sunnypilot - Version Latest (2022-09-12)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* FIXED: Honda Nidec models not gaining speed when longitudinal engaged

sunnypilot - Version Latest (2022-09-11)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: Hyundai Enhanced SCC now forwards FCW and AEB signals and commands from radar to car
* RE-ENABLED: MADS Status Icon toggle

sunnypilot - Version Latest (2022-09-10)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: RAM improvement implementation thanks to realfast!
* DISABLED: Chrysler/Jeep/Ram with Custom Stock Longitudinal Control
* DISABLED: Volkswagen MQB & PQ with Custom Stock Longitudinal Control

sunnypilot - Version Latest (2022-09-09)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.09-07:35:00:GMT - 0.8.17
* NEW❗: MADS now supporting General Motors (GM)
* ADDED: Custom Stock Longitudinal Control - Volkswagen
    * MQB & PQ
* ADDED: Reverse ACC Change
    * ACC +/-: Short=5, Long=1
* ADDED: Custom Stock Longitudinal Control
    * Hyundai/Kia/Genesis
    * Honda Bosch
* ADDED: Hyundai: 2015-16 Genesis resume from standstill fix (commaai:openpilot#25579) - pending PR
* Vision Turn Speed Control re-enabled
* Disable Onroad Uploads toggle re-enabled

sunnypilot - Version Latest (2022-09-08)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.08-04:05:00:GMT - 0.8.17
* NEW❗: Block lane change initiation while brake is pressed

sunnypilot - Version Latest (2022-09-07)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.08-04:05:00:GMT - 0.8.17
* NEW❗: Display End-to-end longitudinal 🌮 on screen
    * NEW❗: Hold DISTANCE button on the steering wheel for 1 second to switch between E2E Long and ACC mode
    * Enable toggle on the driving screen to switch between modes with End-to-end longitudinal
    * Only applicable to cars with openpilot longitudinal control
* NEW❗: Block lane change initiation while brake is pressed
* REMOVED: Dynamic Lane Profile - upstream laneless model is now on by default
* REMOVED: hyundai: consistent start from stop (commaai:openpilot#25672) - pending PR

sunnypilot - Version Latest (2022-09-06)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.06 - 0.8.17
* NEW❗: Display useful metrics above the chevron that tracks the lead car
    * Under "SP - Visuals" menu
    * Only applicable to cars with openpilot longitudinal control
* ADDED: hyundai: consistent start from stop (commaai:openpilot#25672) - pending PR
* FIXED: Vienna speed limit interface now scales properly with the outer box
* REMOVED: Hyundai long improvements (commaai:openpilot#25604) - closed PR

sunnypilot - Version Latest (2022-09-05)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.03 - 0.8.17
* NEW❗: Speed Limit Control (SLC) interface integrated with upstream
* NEW❗: Speed limit from active navigation is now prioritized for Speed Limit Control
* NEW❗: MUTCD (U.S.) or Vienna (E.U.) speed limit interfaces can now be selected under "SP - Controls"

sunnypilot - Version Latest (2022-09-04)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.03 - 0.8.17
* FIXED: Gap Adjust Cruise status now displays properly on screen
* FIXED: mapd - missing index in list caused mapd to crash
* REMOVED: Temporary removed Vision Turn Speed Control

sunnypilot - Version Latest (2022-09-03)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.03 - 0.8.17
* ADDED: New border colors for different operation engagements
* ADDED: UI: Show barrier when car detected in blind spot
    * Only applicable to cars that have BSM detection with openpilot
* FIXED: Cruise Cancel button no longer display prompt if cruise not engaged
* TWEAKED: Update changelogs on startup in Settings -> Software -> Version
* REMOVED: Upload Raw Logs and Full Resolution Videos toggles

sunnypilot - Version Latest (2022-08-31)
========================
* UPDATED: Synced with commaai's master branch - 2022.08.31 - 0.8.17
* ADDED: New border colors for different operation engagements
* ADDED: UI: Show barrier when car detected in blind spot
    * Only applicable to cars that have BSM detection with openpilot
* FIXED: Cruise Cancel button no longer display prompt if cruise not engaged
* REMOVED: Upload Raw Logs and Full Resolution Videos toggles

sunnypilot - Version 0.8.16 (2022-07-16)
========================
* Sync with commaai's master branches
* NEW❗: Add toggle to pause lateral actuation below 30 MPH / 50 KM/H
* IMPROVED: Better controls mismatch handling
* IMPROVED: Less frequent Low Memory alert
* IMPROVED: Only allow lateral control when in forward gears
* IMPROVED: Better alerts handling on gear changes

sunnypilot - Version 0.8.14-1.3 (2022-06-29)
========================
* Hyundai/Kia/Genesis
    * NEW❗: MADS: Add GAP/Distance button on the steering wheel to engage/disengage
        * To engage/disengage MADS: Hold the button for 0.5 second
* NEW❗: Dynamic Lane Profile: Add toggle to enable "Laneless for Curves in Auto Lane"
* HOTFIX🛠: Improve Torque lateral control and reduce ping pong for some Toyota cars
    * Torque control: higher low speed gains and better steering angle deadzone logic
* Developer UI: Remove Distance Traveled, replace with Memory Usage %
    * This may have a potential to fix the Low Memory alert that may appear

sunnypilot - Version 0.8.14-1 (2022-06-27)
========================
* HOTFIX🛠: Honda, Toyota, Volkswagen now initialized correctly with Torque Lateral Live Tune

sunnypilot - Version 0.8.14-1 (2022-06-27)
========================
* NEW❗: Added toggle to enable updates for sunnypilot
* HOTFIX🛠: Volkswagen car list now displays properly in Force Car Recognition menu
* REVERTED: Honda - temporary removes CRUISE (MAIN) for MADS engagement
    * LKAS button continues to be used for MADS engagement/disengagement

sunnypilot - Version 0.8.14-1 (2022-06-26)
========================
Visit https://bit.ly/sunnyreadme for more details
* sunnypilot 0.8.14 release - based on openpilot 0.8.14 devel
* "0.8.14-prod-c3" branch only supports comma three
    * If you have a comma two, EON, or other devices than a comma three, visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
* Mono-branch support
    * Honda/Acura
    * Hyundai/Kia/Genesis
    * Toyota/Lexus
    * Volkswagen MQB
* Modified Assistive Driving Safety (MADS) Mode
    * NEW❗: CRUISE (MAIN) now engages MADS for all supported car makes
    * NEW❗: Added toggle to disable disengaging Automatic Lane Centering (ALC) on the brake pedal
* Dynamic Lane Profile (DLP)
* NEW❗: Gap Adjust Cruise (GAC)
    * openpilot longitudinal cars can now adjust between the lead car's following distance gap via 3 modes:
        * Steering Wheel (SW) | User Interface (UI) | Steering Wheel + User Interface (SW+UI)
* NEW❗: Custom Camera & Path Offsets
* NEW❗: Torque Lateral Control from openpilot 0.8.15 master (as of 2022-06-15)
* NEW❗: Torque Lateral Control Live Tune Menu
* NEW❗: Speed Limit Sign from openpilot 0.8.15 master (as of 2022-06-22)
* NEW❗: Mapbox Speed Limit data will now be utilized in Speed Limit Control (SLC)
    * Speed limit data will be utilized in the following availability:
        * Mapbox (active navigation) -> OpenStreetMap -> Car Interface (Toyota's TSR)
* Custom Stock Longitudinal Control
    * NEW❗: Volkswagen MQB
    * Honda
    * Hyundai/Kia/Genesis
* NEW❗: Mapbox navigation support for non-Prime users
    * Visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
* Hyundai/Kia/Genesis
    * NEW❗: Enhanced SCC (ESCC) Support
        * Requires hardware modification. Visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
    * NEW❗: Smart MDPS (SMDPS) Support - Auto-detection
        * Requires hardware modification and custom firmware for the SMDPS. Visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
* Toyota/Lexus
    * NEW❗: Added toggle to enforce stock longitudinal control

sunnypilot - Version 0.8.12-4
========================
* NEW❗: Custom Stock Longitudinal Control by setting the target speed via openpilot's "MAX" speed thanks to multikyd!
    * Speed Limit Control
    * Vision-based Turn Control
    * Map-based Turn Control
* NEW❗: HDA status integration with Custom Stock Longitudinal Control on applicable HKG cars only
* NEW❗: Roll Compensation and SteerRatio fix from comma's 0.8.13
* NEW❗: Dev UI to display different metrics on screen
    * Click on the "MAX" box on the top left of the openpilot display to toggle different metrics display
    * Lead car relative distance; Lead car relative speed; Actual steering degree; Desired steering degree; Engine RPM; Longitudinal acceleration; Lead car actual speed; EPS torque; Current altitude; Compass direction
* NEW❗: Stand Still Timer to display time spent at a stop with M.A.D.S engaged (i.e., stop lights, stop signs, traffic congestions)
* NEW❗: Current car speed text turns red when the car is braking
* NEW❗: Export GPS tracks into GPX files and upload to OSM thanks to eFini!
* NEW❗: Enable ACC and M.A.D.S with a single press of the RES+/SET- button
* NEW❗: ACC +/-: Short=5, Long=1
    * Change the ACC +/- buttons behavior with cruise speed change in openpilot
    * Disabled (Stock):  Short=1, Long=5
    * Enabled:  Short=5, Long=1
* NEW❗: Speed Limit Value Offset (not %)*
    * Set speed limit higher or lower than actual speed limit for a more personalized drive.
    * *To use this feature, turn off "Enable Speed Limit % Offset"*
* NEW❗: Dedicated icon to show the status of M.A.D.S.
* NEW❗: No Offroad Fix for non-official devices that cannot shut down after the car is turned off
* NEW❗: Stop N' Go Resume Alternative
    * Offer alternative behavior to auto resume when stopped behind a lead car using stock SCC/ACC. This feature removes the repeating prompt chime when stopped and/or allows some cars to use auto resume (i.e., Genesis)
* IMPROVED: Show the lead car icon in the car's dashboard when a lead car is detected by openpilot's camera vision
* FIXED: MADS button unintentionally set MAX when using stock longitudinal control thanks to Spektor56!

sunnypilot - Version 0.8.12-3
========================
* NEW❗: Bypass "System Malfunction" alert toggle
    * Prevent openpilot from returning the "System Malfunction" alert that hinders the ability use openpilot
* FIXED: Hyundai/Kia/Genesis Brake Hold Active now outputs the correct events on screen with M.A.D.S. engaged

sunnypilot - Version 0.8.12-2
========================
* NEW❗: Disable M.A.D.S. toggle to disable the beloved M.A.D.S. feature
    * Enable Stock openpilot engagement/disengagement
* ADJUST: Initialize Driving Screen Off Brightness at 50%

sunnypilot - Version 0.8.12-1
========================
* sunnypilot 0.8.12 release - based on openpilot 0.8.12 devel
* Dedicated Hyundai/Kia/Genesis branch support
* NEW❗: OpenStreetMap integration thanks to the Move Fast team!
    * NEW❗: Vision-based Turn Control
    * NEW❗: Map-Data-based Turn Control
    * NEW❗: Speed Limit Control w/ optional Speed Limit Offset
    * NEW❗: OpenStreetMap integration debug UI
    * Only available to openpilot longitudinal enabled cars
* NEW❗: Hands on Wheel Monitoring according to EU r079r4e regulation
* NEW❗: Disable Onroad Uploads for data-limited Wi-Fi hotspots when using OpenStreetMap related features
* NEW❗: Fast Boot (Prebuilt)
* NEW❗: Auto Lane Change Timer
* NEW❗: Screen Brightness Control (Global)
* NEW❗: Driving Screen Off Timer
* NEW❗: Driving Screen Off Brightness (%)
* NEW❗: Max Time Offroad
* Improved user feedback with M.A.D.S. operations thanks to Spektor56!
    * Lane Path
        * Green🟢 (Laneful), Red🔴 (Laneless): M.A.D.S. engaged
        * White⚪: M.A.D.S. suspended or disengaged
        * Black⚫: M.A.D.S. engaged, steering is being manually override by user
    * Screen border now only illuminates Green when SCC/ACC is engaged

sunnypilot - Version 0.8.10-1 (Unreleased)
========================
* sunnypilot 0.8.10 release - based on openpilot 0.8.10 `devel`
* Add Toyota cars to Force Car Recognition

sunnypilot - Version 0.8.9-4
========================
* Hyundai: Fix Ioniq Hybrid signals

sunnypilot - Version 0.8.9-3
========================
* Update home screen brand and version structure

sunnypilot - Version 0.8.9-2
========================
* Added additional Sonata Hybrid Firmware Versions
* Features
    * Modified Assistive Driving Safety (MADS) Mode
    * Dynamic Lane Profile (DLP)
    * Quiet Drive 🤫
    * Force Car Recognition (FCR)
    * PID Controller: add kd into the stock PID controller

sunnypilot - Version 0.8.9-1
========================
* First changelog!
* Features
    * Modified Assistive Driving Safety (MADS) Mode
    * Dynamic Lane Profile (DLP)
    * Quiet Drive 🤫
    * Force Car Recognition (FCR)
    * PID Controller: add kd into the stock PID controller
