# FunnyPilot Development Notes

## SSH Access

**Home network (direct):**
```bash
ssh comma@192.168.86.31
```

**Remote / off-network (Tailscale VPN — works from anywhere):**
```bash
ssh -o ProxyCommand="/home/astro/bin/tailscale --socket=/home/astro/.local/share/tailscale/tailscaled.sock nc %h %p" comma@100.93.118.118
```
Device Tailscale IP: `100.93.118.118` (hostname: `comma-740ff8eb`)
Tailscale account: `nohaxjustdoge@`

**Dev server Tailscale** runs as a persistent systemd user service (no root needed):
```bash
systemctl --user status tailscaled   # check status
systemctl --user restart tailscaled  # restart if needed
/home/astro/bin/tailscale --socket=/home/astro/.local/share/tailscale/tailscaled.sock status
```
State: `/home/astro/.local/share/tailscale/` — persists across restarts.
Linger enabled: service auto-starts on boot even without active login session.

**Device Tailscale** runs via systemd (kernel TUN mode) with state in `/data/tailscale/state/`
(survives AGNOS updates). Managed by `/etc/systemd/system/tailscaled.service.d/state.conf`.

**If device Tailscale stops working after an AGNOS update:**
```bash
ssh comma@192.168.86.31  # home network first
sudo systemctl daemon-reload
sudo systemctl restart tailscaled
# If systemd service is gone (AGNOS wiped /etc):
sudo update-alternatives --set iptables /usr/sbin/iptables-legacy
sudo mkdir -p /run/tailscale
sudo /data/tailscale/bin/tailscaled \
  --state=/data/tailscale/state/tailscaled.state \
  --socket=/run/tailscale/tailscaled.sock --port=41641 &
sleep 3
sudo /data/tailscale/bin/tailscale --socket=/run/tailscale/tailscaled.sock up --ssh=false
```

## Git Remotes

- `funnypilot` - git@github.com:jgray-dev/funnypilot.git (push here)
- `origin` - sunnypilot upstream

## Branching Policy (v2.2.0+)

All versions must be separate branches: `funnypilot-X.Y.Z`
In each new branch, modify the CHANGELOG.md file to correspond to what was last modified in the version of the branch.
Additionally, edit CLAUDE.md Key Files section to describe what changes and logic were implemented in what files.

## Deploying to Device

```bash
BRANCH=$(git branch --show-current)
git push funnypilot "$BRANCH:$BRANCH" --force
ssh -o ProxyCommand="/home/astro/bin/tailscale --socket=/home/astro/.local/share/tailscale/tailscaled.sock nc %h %p" comma@100.93.118.118 \
  "cd /data/openpilot && git fetch funnypilot && git checkout $BRANCH && git reset --hard funnypilot/$BRANCH && sudo systemctl restart comma"
```

**Push scripts:** Only create a `PUSH<version>.sh` if the user explicitly requests it (e.g. device is offline and a manual-deploy script is needed). Do not create them by default.

## Key Files

- `FUNNYPILOT_VERSION` - Version number only. No changelog.

### v3.3.6 Changes (based on funnypilot-3.3.5)

Lateral: the "spend the delay window on smoothing" feel returns WITHOUT
violating the v3.3.2 post-mortem. The EMA failed because it filtered the
KNOTS (redistributing maneuver onset); v3.3.6 instead smooths only the
100 Hz path BETWEEN unchanged knots — every model knot value is still
reached exactly on the validated delta/5 schedule, so onset cannot move.

- `selfdrive/controls/lib/lat_smooth.py` — LatSmoother gains method
  SPLINE (default; LINEAR = the bit-exact validated ramp, kept for A/B).
  Per 50 ms segment, g(alpha) is a monotone cubic Hermite: entry slope =
  the output's realized slope carried across the knot (C1 through
  sustained maneuvers — the linear scheme's 20 Hz slope staircase was
  the residual harshness); exit slope aimed at a plan lookahead
  (next_curv_est), central-difference (next - prev)/(2T). Both slopes
  clamped to the Fritsch–Carlson monotone box [0, 3]*secant =>
  provably monotone, in-bracket, knot-exact at alpha=1 (bracket also
  hard-clamped — LOAD-BEARING). delta ~= 0 => exactly flat (a flat
  desire NEVER creeps — the exact v3.2.12 failure, unit-tested).
  Reversal/flat entry restarts from zero slope. KEY compat invariant:
  on a constant-rate maneuver the carried slope equals the secant and
  the spline collapses to alpha — bit-identical to delta/5. Slope carry:
  one alpha-saturated frame keeps the aimed exit slope (dg(1) == c1);
  2+ saturated frames (real model stall, output holding cur) decay it to
  0 — do NOT "simplify" this to zeroing at alpha >= 1, that reintroduces
  a mid-corner ease-in restart (regression-tested; float luck can hide
  it because alpha may land epsilon under 1.0). The v3.3.2
  DO-NOT-REINTRODUCE note stands, amended to state why the spline is not
  causal filtering (touches no knot values/times, uses the plan's
  future, never past outputs).
- `selfdrive/controls/controlsd.py` — `_model_lookahead_curv` revived
  from v3.2.2 (deleted in 3.2.9e): get_curvature_from_plan at
  lat_delay + 2*DT_MDL, i.e. one model step past the action horizon —
  a pure read of the already-published plan inside the lagd delay
  window. Returns None on any doubt (short plan, non-finite,
  |c| > MAX_CURVATURE) => spline falls back to the plain secant. Fed to
  lat_smooth.update only on model frames. Marker comment `v3.3.6`
  (code_controlsd grep target updated to match).
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> "3.3.6";
  controlsd marker "v3.2.10" -> "v3.3.6"; new ("SPLINE", lat_smooth.py)
  marker row.
- `FUNNYPILOT_VERSION` -> 3.3.6.
- `selfdrive/controls/lib/tests/test_lat_smooth.py` — 19 cases: LINEAR
  keeps the exact validated-schedule tests; SPLINE adds constant-ramp
  bit-compat, knot-exact-on-time (arbitrary knot sequences),
  flat-never-creeps (even with a lookahead announcing a turn),
  monotone/in-bracket under adversarial lookaheads (±10, NaN, None),
  C1 no-rate-step at knots (vs LINEAR's measured 3x kink), apex settle,
  bounded deviation from the linear path (<= 0.25*delta sub-period),
  direction-reversal bracket, saturated-frame slope-carry regression.
- Verified in sim (perfect / t-invariant / absent lookahead): corner
  profile peak jerk 8.0 -> 2.4 (perfect la), 6.3 (t-invariant), 4.5
  (absent), knot timing bit-exact in all cases; on noisy steady-state
  knots the spline is ~equal-or-better with a lookahead present. NOTE:
  with lookahead absent AND zigzag noise the spline's peak jerk is
  slightly worse than LINEAR (45 vs 40 on synthetic worst case) —
  acceptable: controlsd supplies the lookahead whenever the plan
  parses, and clip_curvature remains the hard downstream limit.

### v3.3.5 Changes (based on funnypilot-3.3.4)

Single fix: soundd survives a runtime audio-stream death instead of
crashing into the "Communication Issue between Processes" takeover alert.

- `selfdrive/ui/soundd.py` — `soundd_thread` restructured from stock's
  `assert stream.active` (which crashed the process on any PortAudio
  stream death; the `@retry` on `get_stream` only covered the initial
  open) to an outer reinit loop: poll `while stream.active`, and on
  inactivity exit the `with` (closing the stream) and reopen via the
  existing `get_stream` (re-terminates/re-initializes portaudio,
  `@retry(attempts=10, delay=3)`). `Ratekeeper` moved out of the stream
  scope so reinit doesn't reset pacing.
- Failure visibility preserved BY DESIGN — do not "improve" this into an
  unbounded loop: a stream that can't reopen exhausts get_stream's retry
  and the raise kills the process (watchdog alert fires as before), and
  5 consecutive streams living < 10 s raise RuntimeError for the same
  reason. The fix only rides out transient hiccups; a dead speaker must
  still be loud (via the alert), since audible alerts are safety.
- `FUNNYPILOT_VERSION` -> 3.3.5; `sunnypilot/navd/nav_webserver.py`
  `EXPECTED_VERSION` -> "3.3.5" (`_CODE_MARKERS` untouched).

### v3.3.4 Changes (based on funnypilot-3.3.3st)

Single fix: the hidden cruise governor no longer lifts on lead detection.

- `selfdrive/controls/lib/longitudinal_planner.py` — removed the
  `not following` term from the `HIDDEN_CRUISE_OFFSET` gate. In 3.3.3st,
  detecting a lead (MPC source `lead0`/`lead1`) restored the ungoverned
  set speed, so the car would run up to ~7% (~6 mph at highway speeds)
  over the governed max to stay with a lead, and the one-frame-lagged
  `following` flag + LeadGrace's v_ego floor made the overspeed sticky
  through lead flicker/handoff. The governor now shaves `v_cruise`
  whenever it's initialized and no forced decel is active, lead or not.
  This is speed-domain only (the ARCHITECTURE RULE holds): it lowers the
  MPC's cruise-obstacle ceiling and can never weaken lead braking, which
  the MPC's lead constraint owns.
- The governor itself is UNCHANGED and stays hidden: `HIDDEN_CRUISE_OFFSET
  = 0.93`, no UI/car-state exposure, no settings toggle (per the 3.3.3st
  USER NOTE below — that remains the point of "hidden").
- `FUNNYPILOT_VERSION` -> 3.3.4; `sunnypilot/navd/nav_webserver.py`
  `EXPECTED_VERSION` -> "3.3.4" (`_CODE_MARKERS` untouched — the
  `HIDDEN_CRUISE_OFFSET` marker still matches).

### v3.3.3st Changes (based on funnypilot-3.3.3)

Stable "st" cut of 3.3.3 (lateral/SLA/long stack byte-identical) plus
three independent hands-off features.

- `selfdrive/controls/lib/longitudinal_planner.py` — `HIDDEN_CRUISE_OFFSET
  = 0.93` (v3.3.3st marker) reintroduces the pre-3.2.6e hidden cruise-only
  speed offset (that rewrite removed the old 0.9x `HIDDEN_CRUISE_OFFSET`
  entirely for "predictability" — see the v3.2.6e section below; this is
  a deliberate reintroduction at -7%, not a regression). Applied to
  `v_cruise` right after the `force_slow_decel` clamp and before it's fed
  to `self.mpc`/`LeadGrace` — i.e. before the controls layer ever sees
  it, and nowhere near anything the UI reads. Gated off via the existing
  one-frame-lagged `following = self.mpc.source in (lead0, lead1)` check
  (already computed for `LeadGrace`) and `not force_slow_decel` — so lead
  following/braking and forced safety decels are completely untouched;
  the shave only ever applies while freely tracking the set speed. The
  old `_following_v2`/`self.source` plumbing this rode on pre-3.2.6e is
  gone, so the gate is expressed against the current single-authority
  MPC source instead — same intent, current architecture.
- `sunnypilot/auto_updater/manager.py` — NEW `AutoUpdater` class/process
  (`v3.3.3st` marker), registered in `system/manager/process_config.py`
  as `PythonProcess("auto_updater", "sunnypilot.auto_updater.manager",
  only_offroad)`. Offroad-ness comes for free from the process gate
  (killed the instant the car goes onroad, so the timer can't span a
  drive); internally tracks continuous `deviceState.networkType == wifi`
  via a 1 Hz `SubMaster`. After 15 continuous minutes on WiFi, does
  exactly what the Settings buttons do: `ModelManager_DownloadIndex` <-
  the currently active bundle's index (re-verifies/redownloads only the
  artifacts whose hash actually changed upstream — a no-op most cycles),
  and if a map region is configured (`OsmLocal`), `OsmDbUpdatesCheck` ->
  True. Re-arms every 15 minutes so a long parked session keeps both
  current without the user opening Settings. Skips the model refresh if
  a download is already in flight (`ModelManager_DownloadIndex` already
  set) and skips the map refresh entirely if no region was ever
  configured (nothing meaningful to refresh).
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — NEW
  `LagdElement` (v3.3.3st marker) reads `sm['liveDelay']` and renders
  `lateralDelay` (the vetted, block-averaged value — same one
  `LagdToggle`'s live-learn mode feeds into `controlsd`) to 3 decimals,
  green when `status == estimated`, red when `invalid`, white while
  still `unestimated`/warming up.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py` —
  instantiates `self.lagd_elem` and appends it to the bottom dev-UI bar
  (right after lead speed, before the torque/angle-specific elements),
  gated on `sm.valid['liveDelay']` (already subscribed via
  `ui_state.py`'s `sm_services_ext`, so no new subscription needed).
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> "3.3.3st";
  three new `_CODE_MARKERS` rows (hidden cruise governor, lagd dev-UI
  readout, offroad-wifi auto-updater).
- `FUNNYPILOT_VERSION` -> 3.3.3st.
- USER NOTE: the hidden cruise offset is intentionally undocumented in
  any user-facing UI — do not add a settings toggle or display for it
  without being asked; that's the point of "hidden."

### v3.3.3 Changes (based on funnypilot-3.3.2)

Original SLA arrow activation restored + pre-zone gas gating + logging
declutter. Lateral untouched. ARCHITECTURE RULE upheld: SLA stays
speed-domain; the one new accel-adjacent output is a THROTTLE-ONLY clamp
(accel_clip[1] -> coast accel, floor untouched) — it can coast, never
brake, and lead braking passes through unchanged.

- `sunnypilot/.../speed_limit/speed_limit_assist.py` — REWRITTEN activation
  (v3.3.3 marker = code grep target): tap-to-adopt REMOVED; states
  disabled -> preActive (6 s `PRE_ACTIVE_WINDOW`, entered on engage-with-
  limit, on zone change while inactive, or limit first appearing) ->
  active on a cruise press in the arrow's direction (`_confirm_pressed`
  consumes 0.5 s-valid button RELEASES recorded by update_car_state;
  set==limit auto-activates, incl. from inactive at any time). Activation
  ADOPTS the current set speed unchanged — `_activate` derives the ratio
  from the cluster (`_set_ratio_from_cluster`), NOTHING is written to the
  cruise speed (no jump; the confirm press is swallowed). ACTIVE behavior
  = the v3.2.6e stack unchanged (cluster-is-target, ratio re-derive,
  idempotent snap, deactivate only on disengage/mode off). NEW
  `gas_gate_active`: active + upcoming lower zone + within coast envelope
  ((v²-target²)/(2*GATE_COAST_ACCEL 0.35) + target*1.5 s buffer), target =
  next_limit_final*(1+ratio). update() gained kwargs
  next_speed_limit_final/next_distance (default 0 — old calls safe).
- `sunnypilot/.../speed_limit/speed_limit_resolver.py` — upstream early
  limit-switch (LIMIT_ADAPT_ACC block, FIXME'd upstream) REMOVED: it
  changed the resolved limit pre-boundary, which would fire the SLA snap
  early = braking before the zone. Now exposes `next_speed_limit`,
  `next_speed_limit_final` (static offset via new `_offset_for_limit`),
  `distance_to_next_limit` continuously; resolved limit flips exactly at
  the boundary.
- `sunnypilot/selfdrive/car/cruise_ext.py` — restored old directional
  swallow: `update_speed_limit_assist` computes req_plus/req_minus
  (helpers.compare_cluster_target); `..._pre_active_confirmed` swallows
  accel-press when req_plus / decel-press when req_minus during
  preActive (long presses pass through). NO write on activation (adopt);
  the snap fires ONLY on zone change while already active
  (limit*(1+ratio)). `selfdrive/car/cruise.py` call site unchanged.
- `selfdrive/controls/lib/longitudinal_planner.py` — after update_targets:
  `if self.sla.gas_gate_active: accel_clip[1] = min(accel_clip[1],
  max(accel_coast, accel_clip[0]))` (v3.3.3 marker). Rides the existing
  0.05/frame clip rate limit, so the gate engages/releases smoothly.
- `selfdrive/ui/sunnypilot/onroad/speed_limit.py` — UNCHANGED: the
  preActive pulse + `_draw_pre_active_arrow` (up/down vs set speed) were
  still in the tree keying off assist.state; the state machine simply
  produces preActive again.
- `selfdrive/controls/lib/triage_recorder.py` — idle collapse:
  LatInterpMonitor skips parked seconds (nothing active, sp==0, v<0.5) —
  one {"idle":N} heartbeat/60 s, first driving record carries "idl":N.
  RadarTracksMonitor collapses zero-track seconds the same way (30 s
  heartbeat, cerr accumulated).
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION 3.3.3;
  DIAG_CHECKS consolidated ~31 -> 7 rows: version/branch/clean/`code`
  (single _CODE_MARKERS sweep, 16 greps, fail names the missing marker)/
  `updater` (target+staged one row)/model_bundle/`logs` (dir listing).
  All old code_*/triage_* row ids are GONE — _eval_diag rewritten to
  match.
- Tests: test_speed_limit_assist.py rewritten (30 cases incl. TestGasGate
  + adopt-on-confirm/no-jump assertions);
  test_triage_recorder.py updated + idle-collapse cases (22). Full
  import-light suite 101 green. test_cruise_mode/test_speed_limit_resolver
  need device (ipc_pyx/params) — resolver tests use speedLimitAhead=0 so
  the early-switch removal doesn't affect them.

### v3.2.11 Changes (based on funnypilot-3.2.10)

Preview-budget smoothing enabled — the user's "use the artificial delay to
interpolate" concept via upstream's own plumbing. KEY FINDING: the
models-page software delay (Params `LagdToggleDelay` ~0.35 s; with
`LagdToggle` off the used delay = CP.steerActuatorDelay + LagdToggleDelay
~= 0.50 s, matching triage latDelay) reserved preview that nothing spent —
`LAT_SMOOTH_SECONDS` was zeroed by a sunnypilot base sync (commit 6275006).
Its lag is PRE-PAID: modeld adds it to the action horizon and controlsd
adds it to lat_delay, so enabling it costs no reaction time.

- `selfdrive/modeld/modeld.py` — LAT_SMOOTH_SECONDS 0.0 -> 0.2 (EMA on
  action.desiredCurvature; existing 2.5 m/s^3 jerk clamp unchanged).
  Smooths the KNOTS that LatSmoother then spreads per-frame.
- `selfdrive/controls/controlsd.py` — triage ctx gains `latDelayEst`
  (liveDelay.lateralDelayEstimate) beside `latDelay` (in use). est <<
  used => steering early => trim the models-page delay (tell user).
- `FUNNYPILOT_VERSION` -> 3.2.11; nav_webserver EXPECTED_VERSION ->
  3.2.11 + `code_smoothsec` check (`LAT_SMOOTH_SECONDS = 0.2`).
- USER GUIDANCE recorded: pair with models-page software delay 0.35 ->
  ~0.15 so total preview stays ~0.5 s; trim further if turn-in feels
  early. 3.2.10 vs 3.2.11 is a clean A/B of exactly this change.

### v3.3.2 Changes (based on funnypilot-3.3.1)

REVERT of v3.2.12 adaptive smoothing. POST-MORTEM, do not reintroduce
causal filtering (EMA/FIR) on lateral knots in ANY form: an EMA
REDISTRIBUTES a maneuver across the window rather than delaying it — the
command creeps through partial values ("2.5 before the 5") starting at
the shifted sample point, so turn-in onset moves EARLIER however the
horizon is compensated. "Total-preserving" is a steady-state phase
argument; onset shape is what the driver feels. User had to inflate the
delay knob to fight it => sloppy; no knob value fixes it. The delay
window's ONLY validated use is delay-compensated knots + LatSmoother's
in-period delta/5 interpolation.

- `sunnypilot/modeld_v2/modeld.py` — adaptive tau + lat_sample_delay
  removed; `lat_delay = model.lat_delay + model.LAT_SMOOTH_SECONDS`
  (upstream additive bundle-override semantics) for both network input
  and action horizon; `lat_smooth_active` now just mirrors the override
  (kept for the get_action getattr + tests). `v3.3.2` marker comment =
  code_smoothrev grep target.
- `selfdrive/modeld/modeld.py` — same revert; LAT_SMOOTH_SECONDS = 0.0
  constant additive; `lat_smooth_s` kwarg on get_action_from_model kept
  (defaults to the 0.0 constant, harmless).
- `selfdrive/controls/lib/lat_smooth.py` — smooth_seconds_for_delay +
  LAT_SMOOTH_FRACTION/MAX removed; DO-NOT-REINTRODUCE note in its place;
  LatSmoother untouched. Budget tests removed from test_lat_smooth.py.
- `selfdrive/controls/controlsd.py` — UNCHANGED from 3.2.12 (lat_delay =
  liveDelay.lateralDelay): correct with the EMA gone, and keeps the fix
  for the pre-existing modeld_v2 misalignment.
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> 3.3.2;
  code_smoothsec -> code_smoothrev (greps v3.3.2 in modeld_v2).
- `FUNNYPILOT_VERSION` -> 3.3.2. USER ACTION: restore the models-page
  delay to the preferred per-model value (~0.35); the inflated value was
  compensation for the reverted artifact.

### v3.3.1 Changes (based on funnypilot-3.3.0e)

Verified + evidenced radar-tracks enable. ROOT CAUSE found in upstream
enable_radar_tracks: write response fetched with timeout=0 and never
checked, no read-back — "success" (radarUnavailable=False) reported even
on a NACKed write => 3.3.0e logs full of lead distances with n==0.
Lateral (3.2.12 adaptive smoothing + LatSmoother) user-confirmed good;
carried unchanged.

- `opendbc_repo/opendbc/sunnypilot/car/hyundai/enable_radar_tracks.py` —
  REWRITTEN: write ack checked (real timeout), post-write read-back is
  the ONLY success criterion (verify == TRACKS_ENABLED_CONFIG or tracks
  bit set); radar identity DIDs read in-session (0xF181/0xF187/0xF100);
  every step JSONL-appended to /data/funnypilot_triage/radar_enable.jsonl
  via best-effort `_fp_log` (512K rotate). NOTE: upstream compared
  current_config (6 bytes, data[3:]) against a 5-byte constant — the
  "already enabled" check could never fire; fixed by comparing both forms.
- `selfdrive/controls/lib/triage_recorder.py` — RadarTracksMonitor gains
  `log_identity(CP)`: one radar_identity record at radard startup
  {car, radarUnavailable, fw: all carFw ECUs incl. fwdRadar}.
- `selfdrive/controls/radard.py` — calls triage.log_identity(CP).
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> 3.3.1; new
  `code_radaren` grep + `triage_radaren` (enable handshake inline).
- `FUNNYPILOT_VERSION` -> 3.3.1.
- `opendbc_repo/opendbc/sunnypilot/car/hyundai/tests/test_enable_radar_tracks.py`
  — NEW (5 cases, scripted FakeQuery): the NACKed-write-must-fail case is
  the regression test for the upstream bug.
- TRIAGE ORDER for the user's logs: radar_enable.jsonl session:false =>
  bus/wiring; write_ack:false or verify unchanged => payload 0142
  rejected, use ident block to pick alternate; enabled:true + tracks
  n>0 => working.

### v3.3.0e Changes (based on funnypilot-3.2.12)

Radar tracks enabled + logged for the K5 (DL3). KEY WIRING FACTS: the
sunnypilot base auto-enables Mando radar tracks via
`opendbc/sunnypilot/car/interfaces._initialize_radar_tracks` (UDS write to
0x7D0, called from opendbc car_helpers.get_car -> setup_interfaces with
can_recv/can_send at fingerprint time) — gated on
`HyundaiFlags.MANDO_RADAR`, which KIA_K5_2021 lacked. The enable is
LONG-MODE-INDEPENDENT (runs before long control matters; 0x7D0 TX is in
panda safety unconditionally; radard is only_onroad regardless) — stock
ACC suffices for log collection. Architecture note: card publishes
liveTracks (RadarData) from the radar interface; radard SUBSCRIBES to
liveTracks and does clustering/lead fusion.

- `opendbc_repo/opendbc/car/hyundai/values.py` — KIA_K5_2021 flags gain
  `HyundaiFlags.MANDO_RADAR` (v3.3.0e marker comment): adds the
  hyundai_kia_mando_front_radar DBC + triggers the ignition-time enable.
  Radar declining the write => radarUnavailable stays True, stock
  behavior. Hyundai platform tests green (13 + 383 subtests).
- `selfdrive/controls/lib/triage_recorder.py` — NEW `RadarTracksMonitor`:
  1 Hz radar_tracks.jsonl {n, nmin, nmax, pts (3 closest [dRel,yRel,
  vRel]), l1/l2 ([dRel,vLead,aLeadK] or null), cerr}. Duck-typed,
  try/excepted end to end (garbage-input test).
- `selfdrive/controls/radard.py` — instantiates the monitor, samples once
  per loop after RD.publish with sm['liveTracks'] + RD.radar_state.
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> 3.3.0e; new
  `code_radartrk` grep + `triage_radar` info rows.
- `FUNNYPILOT_VERSION` -> 3.3.0e.
- TRIAGE: n==0 through traffic-laden drive => enable rejected (next step:
  alternate UDS payload); healthy is n≈5-25 with plausible distances.

### v3.2.12 Changes (based on funnypilot-3.2.11; 3.2.11 SUPERSEDED — do not flash)

Delay-funded ADAPTIVE smoothing. CRITICAL FACTS learned here (do not
regress): (1) custom Model Manager bundles run through
sunnypilot/modeld_v2 (modeld_tinygrad NativeProcess), NOT
selfdrive/modeld — lateral feel changes must be made THERE (3.2.11 missed
this and also misaligned controlsd by +0.2 s; superseded). (2) The
models-page delay (0.29-0.39 per model) is a USER-owned knob that feeds
the model network via lateral_control_params — never require changing it
and never change what the network sees. (3) latDelay in triage ctx DRIFTS
(0.478->0.497) => it is the live learner's measurement; the K5's true
command-to-response lag is ~0.5 s. (4) modeld_v2 has a per-bundle 'lat'
smoothing override (default 0) and a generation >= 10 gate on
smooth_curvature.

- `selfdrive/controls/lib/lat_smooth.py` — `smooth_seconds_for_delay`:
  tau = clip(0.4 * delay_in_use, 0, 0.3); returns 0.0 for any degenerate
  input (None/NaN/inf/<=0/str). LAT_SMOOTH_FRACTION/LAT_SMOOTH_MAX_S.
- `sunnypilot/modeld_v2/modeld.py` — per-loop `lat_smooth_active` (bundle
  'lat' override if > 0, else adaptive; 0 when generation < 10 so nothing
  steers early without the EMA); action sampled at
  max(lat_delay - tau, DT_MDL) + DT_MDL; EMA uses tau (getattr fallback
  for direct-call tests); network input lateral_control_params keeps the
  FULL user delay.
- `selfdrive/modeld/modeld.py` — same total-preserving scheme for the
  stock daemon; LAT_SMOOTH_SECONDS constant back to 0.0 (fallback only);
  get_action_from_model gained `lat_smooth_s` kwarg.
- `selfdrive/controls/controlsd.py` — setpoint alignment lat_delay =
  liveDelay.lateralDelay directly (sample-earlier + EMA-lag = total);
  removed the modeld constant import (which had silently ignored
  modeld_v2 bundle overrides).
- `FUNNYPILOT_VERSION` -> 3.2.12; EXPECTED_VERSION -> 3.2.12;
  `code_smoothsec` greps smooth_seconds_for_delay in modeld_v2.
- tests: budget scaling/cap/degenerate-safety in test_lat_smooth.py.

### v3.2.10 Changes (based on funnypilot-3.2.9e)

Restores the validated delta/5 lateral feel; deletes the failed 3.2.9e
PlanRider after one drive ("two 45-degree bites instead of ten 9-degree
ones"). POST-MORTEM, do not repeat: get_curvature_from_plan's output is
nearly t-invariant inside a curve, so advancing the sampling horizon
between model frames produces ~zero motion and each new plan lands the
whole 50 ms of turn progression as ONE step — the 20 Hz staircase reborn
with its largest steps in sharp turns. Idealized ramp-plan tests are NOT
evidence of on-road smoothness; smoothness must be guaranteed by
construction (spread the knot delta across the period).

- `selfdrive/controls/lib/lat_smooth.py` — NEW. `LatSmoother`: on each
  20 Hz model action, prev <- last OUTPUT, cur <- action; each 100 Hz
  frame emits prev + clip(elapsed/T_MODEL + PHASE_LEAD(0.2), 0, 1) *
  (cur - prev). Bit-compatible with the validated 3.1.0e schedule at
  healthy cadence; provably moves every frame; in-bracket; continuous at
  any cadence; holds cur on model stall; NaN-safe; stdlib-only.
  `health_frames` = realized control-frames-per-model-frame (0-5).
- `selfdrive/controls/controlsd.py` — LatSmoother wired (update with
  action.desiredCurvature + sm.updated['modelV2']; reset(self.curvature)
  when lat inactive). Marker for code_controlsd grep: `v3.2.10`.
- `selfdrive/controls/lib/lat_plan_rider.py` + tests — DELETED.
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> 3.2.10;
  `code_latsmooth` replaces `code_planrider`; `code_controlsd` greps
  v3.2.10; `_FEEL_FILES` hashes lat_smooth.py.
- `FUNNYPILOT_VERSION` — 3.2.9e -> 3.2.10.
- `selfdrive/controls/lib/tests/test_lat_smooth.py` — NEW (9 cases incl.
  exact 0.2/0.4/0.6/0.8/1.0 schedule + moves-every-frame invariant).

### v3.2.9e Changes (based on funnypilot-3.2.8)

EXPERIMENTAL deep reset of lateral smoothing: "ride the plan". All knot-
interpolation machinery since 3.0.2e deleted. K5 hardware research
conclusion (recorded in CHANGELOG): LKAS torque interface is 100 Hz (same
as controls; no rate mismatch); real limits are slew (+3/-7 of 384 per
frame), authority, and ~0.5 s measured actuation delay — software must
account for them, they don't make smoothing impossible.

- `selfdrive/controls/lib/lat_plan_rider.py` — NEW. `PlanRider`: stores
  the model plan (orientation.z / orientationRate.z) each model frame and
  every 100 Hz frame evaluates `get_curvature_from_plan` at
  `lat_delay + DT_MDL + plan_age`. Zero added lag, cadence-independent
  (stale plans are ridden up to RIDE_EXTRA_S=0.2 s past nominal, then
  hold), single 2.5 m/s^3 lateral-jerk clamp bounds handoffs/revisions,
  fallback = model action.desiredCurvature (stock) when no valid plan.
  `health_frames` = plan freshness on the old 0-5 scale (dev UI + triage
  compat). Import-light (numpy + drive_helpers + ModelConstants).
- `selfdrive/controls/controlsd.py` — LatInterp/INTERP_METHOD/
  _model_lookahead_curv removed; PlanRider wired (set_plan on model
  update; update each frame; reset(self.curvature) when lat inactive).
  /dev/shm/lat_interp heartbeat + triage hmin/havg now carry plan
  freshness. NOTE: the v3.2.3st controlsd grep marker is gone — the
  code_controlsd diag check now greps `v3.2.9e`.
- `selfdrive/controls/lib/lat_interp.py` + `tests/test_lat_interp.py` —
  DELETED (LINEAR/SETTLE, PHASE_LEAD, settle lookahead, health blend).
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> 3.2.9e;
  `code_planrider` replaces `code_latinterp`; `code_controlsd` greps
  v3.2.9e; `_FEEL_FILES` hashes lat_plan_rider.py instead of lat_interp.
- `FUNNYPILOT_VERSION` — 3.2.8 -> 3.2.9e.
- `selfdrive/controls/lib/tests/test_lat_plan_rider.py` — NEW (9 cases,
  incl. the no-staircase invariant: on a curvature ramp with 20 Hz plan
  updates, consecutive 100 Hz outputs each advance ~a*dt — never flat-
  then-5x). Torque-side scales and the 3.2.8 override gate untouched.

### v3.2.8 Changes (based on funnypilot-3.2.7)

Fix for the "bite then loosen" lateral oscillation (large adjustment ->
hard bite -> immediate ~40% loosen -> repeat at a few Hz). Root cause per
the 3.2.7 analysis: the v3.2.3st driver-override softening limit-cycled
against the controller's own output — wheel-inertia reaction torque during
hard bites latches CS.steeringPressed (HKG STEER_THRESHOLD=150, 5-frame
debounce) with no driver involved, cutting torque to 60% + freezing the
PID integrator; the wheel decelerates, pressed clears, full torque bites
again. NOTE: user's "5 -> 3" torque figures were illustrative, not
measured — the 3.2.7 spe/ovr instrumentation (still active) verifies this
on-road: fixed => spe blips with ovr pinned 1.0; persisting => spe/ovr/
sat/slb say what it actually is (next suspects: EPS/panda rate limits via
slb+sat, then the interpolation bracket via tqx).

- `selfdrive/controls/lib/override_gate.py` — NEW, stdlib-only.
  `OverrideGate(dt)`: dwell-time hysteresis for the override softening.
  Engages only after ENGAGE_TIME=0.4 s of CONTINUOUS steeringPressed;
  releases only after RELEASE_TIME=0.3 s continuous clear. Inertia blips
  (~0.1-0.25 s alternating) can never engage it; per-frame toggling can
  change state at most once. reset() on lateral inactive.
- `selfdrive/controls/lib/latcontrol_torque.py` — `_override_gate` gates
  the 0.6 softening (was raw CS.steeringPressed). The 0.15 s
  FirstOrderFilter ramp and _OVERRIDE_MIN_SCALE=0.6 are unchanged (the
  code_override diag grep still matches); gate reset added to the
  inactive branch. No other lateral changes.
- `FUNNYPILOT_VERSION` 3.2.7 -> 3.2.8; nav_webserver EXPECTED_VERSION ->
  3.2.8 + `code_ovrgate` self-check (`class OverrideGate`).
- `selfdrive/controls/lib/tests/test_override_gate.py` — NEW (8 cases).

### v3.2.7 Changes (based on funnypilot-3.2.6e)

FORENSICS BUILD — the "smoothing feels off after sitting parked" issue
RETURNED on 3.2.5st despite the updater guards. 3.2.7 changes no control
behavior; it records evidence to discriminate hypotheses: (A) code swap
while parked, (B) runtime lat_interp degradation, (C) live-tuning drift.
Root-cause fix goes in the NEXT version after the user copies logs from a
reproduction. When triaging: read code_identity.jsonl first (any
pulse-change record = hypothesis A; hashes catch non-git swaps), then
lat_interp.jsonl hmin around the user's marks.jsonl timestamps (hmin < 4 =
hypothesis B), then compare ctx blocks across days (hypothesis C).

- `selfdrive/controls/lib/triage_recorder.py` — NEW, stdlib-only.
  `TriageRecorder`: rotating JSONL appender (/data/funnypilot_triage, 4MB,
  one .1 backup, every operation try/excepted — returns False, never
  raises). `LatInterpMonitor`: aggregates 100 Hz control-loop samples into
  1 Hz records {n, la, lo, hmin, havg, v, lc, clim, at, ac} + `ctx` every
  10th record via context_fn (exceptions -> ctx=null). hmin is the per-
  second MINIMUM health so transient stalls survive aggregation.
  Post-first-drive: also {sp, spe, ovr, sat, slb, tqx} for the "bite then
  loosen" lateral oscillation — spe counts steeringPressed RISING EDGES
  (edge state persists across record boundaries so one long press = 1),
  ovr is the per-second MIN of latcontrol_torque._override_scale.
  PRIME SUSPECT: v3.2.3st driver-override softening limit cycle (0.6 scale
  == the reported 5->3 torque drop; HKG STEER_THRESHOLD=150 w/ 5-frame
  debounce can be tripped by wheel-inertia reaction torque during hard
  bites; freeze_integrator on steeringPressed compounds it). spe >= 2 +
  ovr == 0.6 during an event confirms; fix planned for 3.2.8.
- `selfdrive/controls/controlsd.py` — instantiates the monitor and calls
  `triage.sample(...)` once per control frame AFTER LaC.update /
  actuators.torque (needs lac_log.saturated + torque; also uses
  lane_change_active, curvature_limited, long_plan.aTarget,
  actuators.accel, CS.steeringPressed, steer_limited_by_safety,
  getattr(LaC, '_override_scale')). context_fn reads liveTorqueParameters
  (latAccelFactorFiltered/frictionCoefficientFiltered), liveParameters
  (angleOffsetDeg/stiffnessFactor), liveDelay.lateralDelay.
- `sunnypilot/navd/nav_webserver.py` — code-identity snapshots: on startup
  (kind=boot) and every 10 min (`_pulse_task`; full record only on change:
  kind=pulse-change, else tiny pulse-ok heartbeat) capturing branch,
  commit, dirty, version, UpdaterTargetBranch, staged branch,
  .overlay_consistent, boot_id, uptime, sha1[:12] of lat_interp.py /
  long_shaping.py / controlsd.py / latcontrol_torque.py (_FEEL_FILES).
  Endpoints: GET /api/logs (list), GET /api/logs/{name}?tail_kb=N
  (whitelist `_TRIAGE_NAME_RE`, tail-read, partial first line dropped),
  POST /api/logs/mark (user marker + note -> marks.jsonl).
  EXPECTED_VERSION -> 3.2.7; new diag rows: code_triage (grep), and
  triage_boot/triage_lat (info: last records inline in Verify).
- `sunnypilot/navd/nav_web/index.html` — "Logs" topbar button + bottom-
  sheet modal: file chips, 128K tail viewer, Copy (clipboard +
  execCommand fallback), purple "⚑ Mark issue now" (prompt for note).
- `FUNNYPILOT_VERSION` — 3.2.6e -> 3.2.7.
- `selfdrive/controls/lib/tests/test_triage_recorder.py` — NEW (10 cases).

### v3.2.6e Changes (based on funnypilot-3.2.5st)

EXPERIMENTAL longitudinal control rewrite: single-authority architecture. The
MPC owns following/braking; ONE shaper stage owns comfort; robustness
heuristics live in the speed domain and can never brake the car. All 3.2.5st
output-path hacks (hidden cruise offset, personality gas gate, lead-cap blend,
FollowingControllerV2 a_override/jerk override, asymmetric output filter) are
removed — do not reintroduce accel-domain overrides downstream of the MPC.

- `selfdrive/controls/lib/long_shaping.py` — NEW, import-light (numpy only).
  `AccelJerkShaper`: asymmetric jerk limiter on the planner's output accel.
  Up-jerk passed per-call (1.4/1.8/2.5 m/s^3 relaxed/standard/aggressive);
  down-jerk interpolated on the DEMANDED accel (`JERK_DOWN_BP [-3.5,-1.0] ->
  [12,4] m/s^3`) so hard braking unlocks a high slew rate immediately —
  comfort can only ever soften throttle, never dilute braking. `bypass=True`
  (FCW) passes the demand through and re-seeds; NaN target -> 0 + re-seed.
  `LeadGrace`: speed-domain lead-loss handling. Arms only after a lead has
  been tracked >= 1.0 s WHILE being the MPC's active constraint (`following`
  flag = mpc.source in lead0/lead1). On loss: cap v_cruise at the lead's last
  speed for 1.5 s (rides out radar flicker), then linear ramp out over 2 s.
  The cap is floored at v_ego — LOAD-BEARING invariant: grace can suppress a
  surge but can never command braking.
- `selfdrive/controls/lib/longitudinal_planner.py` — rewritten output path.
  Flow: reset-state handling (also reseeds shaper + grace) -> allow_throttle /
  coast clip (unchanged) -> SP speed governors -> force_slow_decel (20%
  margin, unchanged) -> LeadGrace cap -> MPC -> get_accel_from_plan (actuator-
  delay-aware, unchanged) -> e2e min-blend (unchanged) -> AccelJerkShaper
  (bypass on FCW) -> rate-limited accel_clip. Removed: HIDDEN_CRUISE_OFFSET,
  personality gas gate, lead-cap blend, fv2 overrides, `_a_target_filter`.
  Kept: FunnyPilot 70% A_CRUISE_MAX table, turn accel limiting.
- `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` — `get_T_FOLLOW`
  now constant time headway per personality (1.25/1.60/2.05 s) + 0.35 s
  cushion below 4 m/s fading out by 14 m/s (replaces the speed-indexed
  mph tables). `COMFORT_BRAKE` 2.0 -> 2.2, `STOP_DISTANCE` 11 -> 7.5,
  removed the closing-rate `inflate_obstacle` hack (double-counted braking
  distance vs `get_stopped_equivalence_factor`), relaxed jerk factor 1.0 ->
  2.0. NOTE: solver .so files in c_generated_code are prebuilt aarch64 —
  these constants are runtime parameters, no codegen needed.
- `selfdrive/controls/lib/longcontrol.py` — bumpless transfer: on
  stopping/starting -> pid entry, the PID integrator is seeded
  (`last_output_accel - a_target - kp*error`, clipped to accel limits) so
  the first frame continues from the last commanded accel. Starting state
  slews from `last_output_accel` toward `CP.startAccel` at
  `STARTING_ACCEL_RATE = 6.0 m/s^3` instead of stepping. State machine
  (`long_control_state_trans`) unchanged.
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` — dropped
  FollowingControllerV2 (import, instance, update call, v_cruise_cap
  application). Speed governor pipeline (SCC-V/SCC-M/SLA/road/weather caps)
  unchanged. `update_targets` signature unchanged.
- `sunnypilot/selfdrive/controls/lib/long_v2/following_v2.py` — DELETED.
  `tuning.py`/`fric.py`/`jerk_filter.py` retained (SCC + tests use them).
- `selfdrive/controls/lib/tests/test_long_shaping.py` — NEW, import-light.
  Key invariants: strong braking (-3.5 m/s^2) reached in <= 6 frames;
  mild braking comfort-limited at 4 m/s^3; FCW bypass immediate; grace cap
  >= v_ego always; short-lived/non-constraining leads never arm grace.
- `selfdrive/controls/tests/test_longcontrol.py` — added
  `test_bumpless_pid_entry`, `test_starting_ramp` (+ `_make_long_control`
  helper with a minimal tuned CP).
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_vision_v2.py` — REWRITTEN
  (was dead: sampled the removed `lateralPlan` service, exception handler
  returned 0 lat accel -> never activated). Now reads
  `modelV2.orientationRate.z * velocity.x` and does POINTWISE corner-speed
  planning: per plan point, v_c = v*sqrt(a_lat_limit/a_pred), allowed-now =
  v_c + 1.2*t_i; raw cap = min. No ENTERING/TURNING state machine — the
  braking point falls out of the time term. `a_lat_target` (2.4 m/s^2,
  LongV2Tuning) with fric influence BOUNDED to [0.7, 1.1]x — liveParameters
  friction is a steering-model value, NOT road grip. Honors the
  SmartCruiseControlVision toggle. update() signature gained `v_cruise`.
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py` — REWRITTEN
  (was dead: parsed MapTargetVelocities as {v,dist,radius}; mapd writes
  [{latitude,longitude,velocity},...]). Parses the real route data
  (nearest-point + forward slice, vectorized haversine, 400 m lookahead);
  cap per point = sqrt(v_curve^2 + 2*1.0*d_eff), d_eff arrives 2 s early;
  `sccm_speed_trim` (0.95) trims mapd speeds; UNtrimmed speed must be
  below cruise for a point to count (straights never become constraints).
  Injectable position/velocities readers for tests. corner_radius_m is a
  display-only estimate v_curve^2/a_lat_target.
- `sunnypilot/selfdrive/controls/lib/long_v2/curve_cap.py` — NEW shared
  `CurveSpeedCap`: 2-frame debounce, cap seeded at current speed on
  activation (returns WITHOUT applying EMA that frame — no step), EMA 0.35
  down-tracking, 2.5 m/s^2 rate-limited release, auto-deactivate once
  released to within 0.5 m/s of cruise. Both SCC controllers are
  SPEED-DOMAIN governors; MPC + shaper own decel.
- `sunnypilot/selfdrive/controls/lib/long_v2/tuning.py` — new fields
  `a_lat_target` (2.4), `sccm_speed_trim` (0.95); `k_sccv`/`k_sccm` kept
  parseable but DEPRECATED (nothing reads them).
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` (SP) — legacy
  v1 `SmartCruiseControl` removed (its output was computed then discarded
  by the LongV2 governor); SCC-V update now takes `v_cruise`.
- `sunnypilot/selfdrive/controls/lib/long_v2/tests/test_scc_v2.py` — NEW,
  import-light (16 cases): envelope math for both controllers, approach
  tightening, in-corner hold, release ramp, passed-curve/no-data, bounded
  fric, cap seeding/debounce.
- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` —
  REWRITTEN. Model: "the cluster set speed IS the SLA target". States:
  disabled -> inactive (silently armed, no prompts/auto-activation) ->
  active/adapting. Activation ONLY by short cruise-down tap (tap-to-adopt):
  ratio = (set_speed - limit)/limit clamped +/-50%, set speed untouched.
  While active, any cluster change re-derives the ratio
  (`_set_ratio_from_cluster`) — IDEMPOTENT with the cruise_ext zone-change
  snap because the snap writes exactly limit*(1+ratio) (this idempotency is
  what fixes the v0.9.8 ratio self-wipe at zone boundaries). Ratio basis is
  `speed_limit_final_last` (posted limit + static offset). Deactivation only
  on long disengage / mode off (ratio reset). Import-light: Params/helpers
  imports are guarded so tests run containerless; `params` injectable.
  `sla_locked` property now == is_active (drives the UI badge).
  preActive/pending/CST/confirm flows and the dead gas-gating accel path
  removed — SLA is SPEED-DOMAIN ONLY (output_a_target = a_ego, display).
  Kept for compat: update() signature, ACTIVE_STATES/ENABLED_STATES,
  published fields, update_car_state (tap classifier, 100 Hz from plannerd).
- `sunnypilot/selfdrive/car/cruise_ext.py` — reads `slaDynamicOffset` from
  longitudinalPlanSP; `update_speed_limit_assist_v_cruise_non_pcm` snaps
  v_cruise to `limit*(1+ratio)` ONLY on zone change while ALREADY active
  (never on activation — adoption keeps set speed).
  `update_speed_limit_assist_pre_active_confirmed(button_type, long_press)`
  now swallows short decel taps while armed (the activation gesture) so the
  tap doesn't decrement the set speed; long presses pass through.
  req_plus/req_minus confirm machinery removed.
- `selfdrive/car/cruise.py` — passes `long_press` to the swallow hook.
- `sunnypilot/selfdrive/controls/lib/speed_limit/tests/test_speed_limit_assist.py`
  — REWRITTEN import-light (FakeParams/FakeEvents, 17 cases): both user
  examples (60/50 -> +20% -> 36 in a 30 zone; drop to 33 -> +10% -> 44 in a
  40 zone), no-jerk adoption, snap idempotency, clamp, long-press/stale-tap
  rejection, limit dropout hold.
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> 3.2.6e; new
  `code_longshape` (`class AccelJerkShaper`), `code_longplan`
  (`v3.2.6e` marker in longitudinal_planner.py), `code_sla`
  (`tap-to-adopt` in speed_limit_assist.py) and `code_sccv2`
  (`class CurveSpeedCap` in curve_cap.py) self-checks.
- KNOWN pre-existing: long_v2/tests/test_physics.py corner-speed/braking
  expectations fail on 3.2.5st too (test-only math mismatch, untouched).
- NOT runnable in CI containers: plant/maneuver tests need the aarch64
  acados solver; validate following/stopping feel on-device.

### v3.2.5st Changes (based on funnypilot-3.2.4e)

Fixes the "interpolation feels disabled after the device sits offroad; reboot
doesn't help; only a ~9MB web reflash fixes it" revert. Root cause: the stock
updater staged its stale `UpdaterTargetBranch` (never updated by the web-UI
flash) into /data/safe_staging while offroad (metered hotspot delays the fetch
until a 3-day timer expires — hence "sits long enough"), and launch_chffrplus.sh
blindly swapped it into /data/openpilot on the next boot. Three independent
guards; no control-path changes (3.2.4e feel carries over byte-identical).

- `launch_chffrplus.sh` — boot-time branch guard. Before installing a finalized
  staged update, compares `git rev-parse --abbrev-ref HEAD` of /data/openpilot
  vs the finalized copy; mismatch (or unreadable current branch) discards the
  staged update by deleting `.overlay_consistent`. Branch switching on this fork
  is therefore ONLY possible via explicit flash (web UI / ssh); the sunnypilot
  settings-menu branch selector can no longer switch branches (intentional).
- `system/updated/updated.py` — (1) startup self-heal: if BASEDIR is on a
  `funnypilot-*` branch and `UpdaterTargetBranch` differs, rewrite the param to
  the flashed branch (runs before the first `set_params`, which would otherwise
  re-persist the stale value). (2) fetch skip: if the target branch isn't in the
  `origin` ls-remote results, skip the fetch cleanly (counts as a successful
  cycle, so no UpdateFailedCount growth / connectivity-needed alerts, which
  hardwared uses as an engagement startup condition).
- `sunnypilot/navd/nav_webserver.py` — `/api/flash` writes `UpdaterTargetBranch`
  (best-effort, lazy Params import) and, after a successful checkout, unmounts
  `/data/safe_staging/merged` and `rm -rf /data/safe_staging` before rebooting.
  Diagnostics: `EXPECTED_VERSION` → 3.2.5st; branch check uses EXPECTED_VERSION;
  new checks `updater_target` (fail on branch mismatch), `staged_branch` (warn if
  a different branch is staged), `updater_off` (info: DisableUpdates state),
  `code_bootguard` (greps FINALIZED_BRANCH in launch_chffrplus.sh),
  `code_updtarget` (greps "adopting flashed branch" in updated.py). The
  `code_controlsd`/`code_chime` greps still match `v3.2.3st` markers — those
  files are unchanged since 3.2.3st; do not "fix" the greps without also
  changing the markers.
- `.gitignore` — `.nav_secrets` and `.funnypilot_nav_cache` (runtime artifacts
  written into /data/openpilot on-device, not created by any code in this repo)
  are ignored so the Verify "Working tree unmodified" check (`git status
  --porcelain`) stays green.
- `FUNNYPILOT_VERSION` — `3.2.3st` → `3.2.5st` (the 3.2.4e branch never bumped
  the file).

### v3.2.3st Changes (based on funnypilot-3.2.2)

Stable cut of 3.2.2 with three on-road follow-ups: smooth lane changes restored,
easier manual override, and the brake-with-lead disengage chime silenced.

- `selfdrive/controls/lib/lat_interp.py` — `update()` gains a `lane_change` arg.
  When True, the SETTLE ease-out is forced off (`g = alpha`, plain linear) so a
  lane change keeps the smooth 3.2.1st feel; the v3.2.2 ease-out otherwise leads
  the curvature as it flattens, which sharpened the S-curve. New
  `test_lane_change_forces_linear` asserts settle(lane_change=True) == linear.
- `selfdrive/controls/controlsd.py` — captures `lane_change_active =
  model_v2.meta.laneChangeState != LaneChangeState.off` (reused from the blinker
  block) and passes it to `lat_interp.update(..., lane_change=lane_change_active)`.
- `selfdrive/controls/lib/latcontrol_torque.py` — driver-override softening. New
  `_OVERRIDE_MIN_SCALE = 0.6`, `_OVERRIDE_TAU = 0.15`, `_override_filter`
  (FirstOrderFilter). While `CS.steeringPressed`, the TOTAL output torque ramps to
  60% so manual takeover needs less force against the firmer v3.2.2 interpolation;
  filtered both directions (no step), exact no-op when not pressed, reset to 1.0 on
  the inactive branch. Composes after the lane-change / re-engage / smooth-stop
  scales. Does NOT touch the interpolation; panda torque limits remain the backstop.
- `opendbc_repo/opendbc/car/hyundai/carcontroller.py` — brake-with-lead chime fix
  (classic-CAN `create_can_msgs`, stock-long button-cancel path). The K5_2021 is
  classic CAN (`CHECKSUM_CRC8`, no CANFD flag) so it cancels via `create_clu11(...
  Buttons.CANCEL)`, NOT `hyundaicanfd.create_acc_cancel`. The brake pedal natively
  cancels the factory cruise, so the redundant CANCEL spam (which drops a tracked
  lead and triggers the car's chime) is suppressed `and not CS.out.brakePressed`.
  CANCEL resumes the instant the brake releases, so cruise can never stay stuck.
  HYPOTHESIS-BASED (the chime is the car's, not an openpilot AudibleAlert) — verify
  on-device that braking still cancels cruise; if the chime persists, re-trace.
- `FUNNYPILOT_VERSION` — `3.2.2` → `3.2.3st`.
- `sunnypilot/navd/nav_webserver.py` — `EXPECTED_VERSION`/`branch` → `3.2.3st`;
  `code_controlsd` greps `v3.2.3st`; new `code_override` (`_OVERRIDE_MIN_SCALE`) and
  `code_chime` (`v3.2.3st` in carcontroller.py) self-checks.

### v3.2.2 Changes (based on funnypilot-3.2.1st)

Robust, cadence-independent lateral interpolation + a new delay-aware "settle"
smoothing method (default), fixing the "interp deactivates after offroad/reboot"
symptom and the dishonest INTERP indicator.

- `selfdrive/controls/lib/lat_interp.py` — NEW module. `LatInterp` class with two
  methods: `LINEAR` (validated uniform delta/5 feel) and `SETTLE` (default). Both
  are TIME-ANCHORED: the sub-frame phase is `alpha = clip(elapsed/T_MODEL +
  PHASE_LEAD, 0, 1)` with `T_MODEL = 0.05` (the model's FIXED 20 Hz period, not a
  measured/EMA period — measuring it from the consumer side re-creates the chronic
  lag) and `PHASE_LEAD = 0.2` (reproduces the old `prev + 0.2*delta` phase advance,
  so the healthy-100 Hz feel is bit-compatible with 3.1.0e+). Replaces the
  open-loop control-frame COUNTER that froze at ~20% of each step when the 100:20
  cadence drifted. A `health` term (`realized control-frames-per-model-frame /
  HEALTH_FULL_FRAMES`, clamped 0..1) blends the output toward the raw model desire
  when sub-frame headroom is lost — worst case degrades to stock, never to a stall.
  SETTLE adds `g(alpha) = alpha + w*alpha*(1-alpha)` (an ease-out that is provably
  ≥ linear and within [0,1]), with `w` from a one-model-step lookahead
  (`_settle_weight`): `w = clip(1 - next_delta/delta, 0, 1)`, 0 when deepening (full
  turn-in responsiveness), →1 when flattening/reversing (gentle apex settle). A
  LOAD-BEARING final clamp to `[min(prev,cur), max(prev,cur)]` (not the curve shape)
  guarantees the output never leaves the model's desire bracket. SETTLE falls back
  to linear below `SETTLE_MIN_SPEED = 6.7` m/s and on any non-finite/out-of-range
  lookahead. NaN model desire → holds last good knot. `reset()` on inactive →
  clean re-engage (first active frame passes the model desire through, clip ramps).
- `selfdrive/controls/controlsd.py` — Removed the `_mp_prev_curv/_mp_cur_curv/
  _mp_frame/_mp_values` frame-counter. Added module-level `INTERP_METHOD = SETTLE`
  (flip to `LINEAR` per-branch to A/B). `self.lat_interp = LatInterp(INTERP_METHOD)`.
  The interpolation block now calls `lat_interp.reset()/update()`; `lat_delay` was
  moved above the block so `_model_lookahead_curv()` (new helper) can sample the
  model plan at `lat_delay + 2*DT_MDL` via `get_curvature_from_plan` (the model's
  `action.desiredCurvature` is its plan at `lat_delay + DT_MDL`). The lookahead is
  only computed for SETTLE on model-update frames. The `/dev/shm/lat_interp` write
  now emits the realized health-frames int (≈5 healthy) instead of a constant "5".
- `selfdrive/controls/lib/tests/test_lat_interp.py` — NEW. Proves the invariants:
  in-bracket, reaches cur at the model frame, SETTLE ≥ linear, linear == old
  schedule, cadence independence, degraded-beats-old-20%-stall, NaN/re-engage/
  low-speed contained. Import-light so it runs without the full openpilot env.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — `LatInterpolElement`
  now an HONEST health gauge: green ≥4, orange 2–3, red 1, white when paused. The
  number is the realized frames-per-model, so degradation shows instead of a
  constant green "5".
- `FUNNYPILOT_VERSION` — `3.2.1st` → `3.2.2`.
- `sunnypilot/navd/nav_webserver.py` — `EXPECTED_VERSION` → `3.2.2`; `branch`
  diagnostic matches `"3.2.2"`; `code_controlsd` greps `v3.2.2`; new `code_latinterp`
  check greps `class LatInterp` in `lat_interp.py`. The interp heartbeat check now
  also serves as the realized-health readout. (Device-side revert cause documented
  in CHANGELOG: check `UpdaterTargetBranch` if the running code isn't 3.2.2.)

### v3.2.1st Changes (based on funnypilot-3.2.1e)

Stable cut of 3.2.1e with a more aggressive post-blinker lateral re-engage.

- `sunnypilot/selfdrive/controls/lib/blinker_pause_lateral.py` — `UNWIND_SETTLE_TIME`
  1.0 → 0.67 s. The wheel must still hold within `UNWIND_THRESHOLD_DEG` (20°,
  unchanged) of center continuously, but only for 0.67 s before the pause releases,
  so lateral re-engages sooner. The excursion-resets-the-timer logic is unchanged.
  The tests in `test_blinker_pause_lateral.py` derive their iteration counts from
  `UNWIND_SETTLE_TIME`, so they keep passing unmodified.
- `selfdrive/controls/lib/latcontrol_torque.py` — Blinker-unwind re-engage ramp
  retuned. New `_REENGAGE_RAMP_START = 0.15` (was an implicit 0%) and
  `_REENGAGE_RAMP_DUR` 4.0 → 3.0 s. The per-frame scale is now
  `_REENGAGE_RAMP_START + (1 - _REENGAGE_RAMP_START) * progress`, i.e. 15% → 100%
  over 3 s. Non-blinker engages keep the `-1e9` sentinel → `progress` saturates to
  1.0 → scale 1.0 (still an exact no-op).
- `FUNNYPILOT_VERSION` — `3.2.1` → `3.2.1st` (stable channel; mirrors the
  funnypilot-3.1.1st convention of carrying the suffix in the version file, which
  `home.py` displays as "FunnyPilot 3.2.1st").
- `sunnypilot/navd/nav_webserver.py` — `EXPECTED_VERSION` `3.2.1` → `3.2.1st` and the
  `branch` diagnostic now matches `"3.2.1st"`, so `/api/diagnostics` stays green on
  the stable branch. The `code_*` grep markers are untouched and still present
  (`v3.2.1e` in controlsd.py, `_REENGAGE_RAMP_DUR`, `UNWIND_SETTLE_TIME`).

### v3.2.1e Changes (based on funnypilot-3.1.2)

- `selfdrive/controls/controlsd.py` — Interpolation hard-fixed at 5-way uniform
  slicing. Removed the dynamic-n gauge math entirely (`_MP_MAX_DELTA`,
  `_MP_HOLD_DECAY`, `n_raw`, `_mp_n_held`, `_mp_n_interp`). Control still does
  `_mp_values[f] = prev + ((f+1)/5)*delta` (identical feel to 3.1.x). Now writes a
  constant `"5"` (engaged) / `"0"` (paused) to `/dev/shm/lat_interp` at the 20 Hz
  model rate as an "interp alive" heartbeat — no more `n,delta` CSV.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — `LatDeltaElement`
  (dCRV) removed. `_read_lat_interp()` parses a single int. `LatInterpolElement`
  simplified to a live indicator: green "INTERP 5" when `latActive`, white "0" when
  paused (no peak-hold, no thresholds).
- `selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py` — dropped the
  `LatDeltaElement` import/instance and its bottom-bar append.
- `selfdrive/controls/lib/latcontrol_torque.py` — Blinker-unwind re-engage ramp.
  On the lateral `active` False→True edge, if a blinker was seen during the
  inactive period, total `output_torque` ramps linearly 0%→100% over
  `_REENGAGE_RAMP_DUR = 4.0` s (+25%/s). Plain engage / standstill release keep the
  `-1e9` sentinel → instant full authority (no-op). Composes after the soft-lane-
  change `scale` (resolves to 1.0 in the pause-release case) and the 0–15 mph
  smooth-stop scaling.
- `sunnypilot/selfdrive/controls/lib/blinker_pause_lateral.py` — Unwind now
  requires the wheel to stay within `UNWIND_THRESHOLD_DEG` (20°) of center
  continuously for `UNWIND_SETTLE_TIME = 1.0` s before releasing the pause (new
  `_unwind_settle_timer`). Brief center crossings (mid-S-curve) reset the timer,
  so lateral no longer re-engages between the two halves of an S.
- `sunnypilot/selfdrive/controls/lib/tests/test_blinker_pause_lateral.py` — gating
  helper resets unwind state per blinker combo; replaced the (dead under
  `UNWIND_MODE=True`) `test_reengage_delay` with `test_unwind_settle_hold` and
  `test_unwind_settle_resets_on_excursion`.
- `sunnypilot/navd/nav_webserver.py` — new `POST /api/diagnostics`. Runs the
  read-only verification checks (`DIAG_CHECKS`) concurrently via asyncio and
  returns `{checks:[{id,name,cmd,output,level,summary}]}`. `_eval_diag()` grades
  each (pass/fail/warn/info): `clean`/`diff` fail if non-empty, `code_*` grep-count
  checks fail if 0, `version` warns off `EXPECTED_VERSION="3.2.1"`. Git commands use
  `-c safe.directory='*'` to avoid dubious-ownership failures. `EXPECTED_VERSION`
  must be bumped per branch.
- `sunnypilot/navd/nav_web/index.html` — "Verify" topbar button + diagnostics modal
  styled as a test suite (PASS/FAIL/WARN pills, per-check command + output) with a
  "Copy Output" button (clipboard API + execCommand fallback for http contexts)
  that formats a plain-text report for pasting back to Claude.

### v3.1.2 Changes (based on funnypilot-3.1.1st)

- `selfdrive/controls/controlsd.py` — INTERP gauge retune (display only; control
  unchanged, still uniform delta/5). `_MP_MAX_DELTA` 0.00006 → 0.000051 (−15%, so a
  15% gentler curve reads the same INTERP). `n_raw` floor 1 → 2 (resting display 3),
  cap 6 → 9 (max display 10). `_mp_n_held` init/reset 1.0 → 2.0.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — `LatInterpolElement`
  colors rescaled for 3–10: green 3–5, orange 6–8, red 9–10.

### v3.1.1st Changes (based on funnypilot-3.1.0e)

- `selfdrive/controls/controlsd.py` — INTERP gauge cap raised: `n_raw` clamp 4 → 6
  (display = n+1, so range now 2..7). Display gauge only; control still uniform delta/5.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — `LatInterpolElement`
  color thresholds rescaled for 1–7 (white=1, green=2–4, orange=5–7). `LatDeltaElement`
  value scaled ×100 (`{display*100:.4f}`) so dCRV reads e.g. "0.0637" not "0.0006".
  Stable release on the validated 3.1.0e feel.

### v3.1.0e Changes (based on funnypilot-3.0.9e)

- `selfdrive/controls/controlsd.py` — Interpolation control path rewritten to
  uniform slicing. The dynamic-n schedule built coarse half-delta steps in the
  common case (n_interp=1 → `[prev, prev+0.5·delta, cur, cur, cur]`): two delta/2
  jumps then flat — the felt "big bite," despite a correct dev-UI value. Control
  now always uses `_mp_values[f] = prev + ((f+1)/5)*delta` for f in 0..4 (uniform
  delta/5 steps, reaching cur at the final sub-frame). `_mp_n_interp` / `_mp_n_held`
  and `_MP_MAX_DELTA` / `_MP_HOLD_DECAY` are retained ONLY to drive the dev-UI
  INTERP gauge; they no longer size control steps. `funnypilot-3.0.9st` is the
  stable snapshot taken just before this change.

### v3.0.9e Changes (based on funnypilot-3.0.8e)

- `selfdrive/controls/lib/latcontrol_torque.py` — Lane change now scales the TOTAL
  output torque (FF + correction), not just the correction. The lane-change motion
  lives in the feedforward, so the prior correction-only scaling never softened the
  maneuver. `_LC_MIN_SCALE` repurposed to 0.45 (total-torque floor). Blinker ON:
  0.45 → 1.0 over 6 s. Blinker OFF: hold 0.45 for 0.5 s, then 0.45 → 1.0 over 2 s
  (alpha² ease-in). The 0% dead zone was removed (zero authority under total scaling).
  FF/correction split and the 3.0.8e NNLC torque-space branch removed — `output_torque
  *= scale` is all that remains. Exact no-op when no lane change is active (scale 1.0).

### v3.0.8e Changes (based on funnypilot-3.0.7)

- `selfdrive/controls/lib/latcontrol_torque.py` — Lane change FF/correction split
  fixed. Was subtracting `torque_from_lateral_accel(linear_ff)` to isolate the
  correction even when NNLC produced `output_torque` from its own neural feedforward
  (torque space) — so a lane change swapped most of the neural FF for the linear FF.
  Negligible behind a lead (in-distribution), rough on open road. Now decomposes via
  `pid.f` (feedforward) vs `pid.p+pid.i+pid.d` (correction) in the PID's native units,
  branching on `extension._nnlc_enabled` for the torque-space vs lat-accel path.
  Exact no-op at `lane_change_torque_scale == 1.0`.

### v3.0.7 Changes (based on funnypilot-3.0.6)

- `selfdrive/controls/controlsd.py` — Interpolation peak-hold with decay.
  `_mp_n_held` (float) tracks effective n_interp: snaps up to `n_raw` instantly,
  decays `_MP_HOLD_DECAY = 0.15`/gate otherwise (5→2 over ~1s). Schedule and shm
  write use the held value. Trigger remains model-desire delta (forward-looking),
  NOT measured steer angle (lagged response — would worsen perceived delay).

- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — `LatInterpolElement`
  now shows a 1-second rolling peak (same pattern as `LatDeltaElement`/dCRV).

### v3.0.6 Changes (based on funnypilot-3.0.5e)

- `selfdrive/controls/controlsd.py` — `_MP_MAX_DELTA` 0.000033 → 0.00006.
  Midpoint between 3.0.4e (0.0001, too high) and 3.0.5e (0.000033, too low).

### v3.0.5e Changes (based on funnypilot-3.0.4e)

- `selfdrive/controls/controlsd.py` — Replace speed-scaled torque threshold
  with `_MP_MAX_DELTA = 0.0001 rad/m` per step. The old `_MP_LAF / v²` formula
  made max_step too large at city speeds. Fixed threshold maps directly to
  observed delta range: display 2 on straights, 3–5 on curves.

- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — "ΔCRV" label
  changed to "dCRV" — Δ renders as "?" in the on-device raylib font.

### v3.0.4e Changes (based on funnypilot-3.0.3e)

- `selfdrive/controls/controlsd.py` — `_MP_MAX_STEP` lowered from 0.10 to 0.04.
  At 30 m/s, the per-step threshold drops from 0.000306 to 0.000122 rad/m, so
  n_interp climbs to 2–4 on moderate curves instead of staying at 1.

### v3.0.3e Changes (based on funnypilot-3.0.2e)

- `selfdrive/controls/controlsd.py` — Dynamic interpolation step count.
  `_mp_n_interp` computed per model gate from `delta / max_step` where
  `max_step = 0.10 * LAF / vEgo²` (speed-scaled, LAF=2.750). Schedule built as
  a 5-value list: frames 0..n_interp interpolate prev→cur, remaining frames hold
  at cur. Every step = delta/(n_interp+1) ≤ max_step. Writes count to
  `/dev/shm/lat_interp` at model-gate rate (~20 Hz, gated to every 20 gates).

- `selfdrive/controls/lib/latcontrol_torque.py` — Post-blinker ease-in ramp.
  `scale = alpha²` replaces linear `scale = alpha` in the 2 s re-engagement ramp.
  Holds correction torque near-zero for ~half the ramp duration, then recovers.

- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — `LatInterpolElement`
  reads `/dev/shm/lat_interp` and renders `INTERP N` in the bottom dev bar.
  Color: white=1, green=2–3, orange=4–5. No params, no compilation.

- `selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py` — Registers
  `LatInterpolElement` and appends it to the torque-state bottom bar.

### v3.0.2e Changes (based on funnypilot-3.0.1)

- `selfdrive/controls/controlsd.py` — Midpoint interpolation between model frames.
  `_mp_prev_curv` / `_mp_cur_curv` / `_mp_frame` track consecutive 20 Hz model
  outputs. At frame index 2 of 5 (10 ms after each model update), a mid-frame
  command = (prev + cur) / 2 is injected. Effective lat command rate: ~40 Hz.
  Zero added lag — no filter. State resets on `CC.latActive = False`.

- `selfdrive/controls/lib/latcontrol_torque.py` — Layers 1 and 2 removed.
  FF FirstOrderFilter and setpoint averaging window both caused phase-lag drift
  on curves. Restored: `setpoint = lat_accel_request_buffer[-delay_frames]`.

### v3.0.1 Changes (based on funnypilot-3.0.0e)

- `selfdrive/controls/lib/latcontrol_torque.py` — `latAccelFactor` locked at 2.750
  in `update_live_torque_params`. Offset and friction still update live; only LAF
  is pinned. Smooth stopping confirmed at 0–15 mph.

- `sunnypilot/navd/nav_web/index.html` — Branch selector replaced with a custom
  scrollable list (removes native `<select>` that overflowed on mobile). Bottom-sheet
  modal, 44 px touch targets, Flash button disabled until branch is selected. Minimal
  dark theme using system fonts.

### v3.0.0e Changes (based on funnypilot-3.0.0)

- `selfdrive/controls/lib/latcontrol_torque.py` — Two interpolation layers using
  `lat_delay` as the smoothing horizon.
  **Layer 1 (FF smoother):** Curvature feedforward is passed through a
  `FirstOrderFilter` with `tau = max(lat_delay, 0.1)`, updated dynamically each
  frame. Path model steps are spread over the vehicle's own response window.
  Friction compensation is added after the filter to stay responsive. Filter is
  seeded from current `ff` on each active→inactive→active transition to prevent
  torque spikes on re-engagement. (`_ff_filter`, `_prev_active`)
  **Layer 2 (setpoint averaging):** The delay-point setpoint lookup is replaced
  with `np.mean` over `±(delay_frames // 3)` frames around the center, clipped to
  buffer bounds. Removes single-frame error spikes without time-shifting the setpoint.

### v3.0.0 Changes (based on funnypilot-2.0.5)

- `selfdrive/monitoring/helpers.py` — All DM timeouts set to 86400 s (24 h). DM
  processes run normally (keeping UI data flows and driverStateV2 intact) but never
  reach terminal state in any realistic driving session. No alerts, no chimes, no
  disengagement.

- `selfdrive/selfdrived/selfdrived.py` — `driverMonitoringState` removed from SubMaster
  subscription list; `add_from_msg(self.sm['driverMonitoringState'].events)` removed.
  Belt-and-suspenders: even if DM somehow accumulated state, its events can't reach
  the event bus.

- `selfdrive/controls/lib/latcontrol_torque.py` — Corner-aware lane change torque.
  Output torque is split into feedforward (path/corner demand) and correction (PID error).
  Only the correction component is scaled during a lane change; feedforward always runs
  at 100% so the car never applies less torque than the curve requires and cannot slip
  to the outside of a turn. On blinker rising edge, correction starts at 10% and ramps
  linearly to 100% over 6 seconds. On blinker falling edge: 0.5 s dead zone (correction
  = 0%), then 0% → 100% ramp over 2 s.
  Constants: `_LC_MIN_SCALE = 0.10`, `_LC_RAMP_DUR = 6.0`, `_POST_DELAY = 0.5`, `_POST_RAMP_DUR = 2.0`.

### v2.0.5 Changes

- `selfdrive/controls/lib/longitudinal_planner.py` — Cruise offset gating, lead
  blend smoothing, and positive-accel filtering for consistent behavior with
  and without leads.
- `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py`,
  `sunnypilot/selfdrive/controls/lib/long_v2/tuning.py` — Follow distance tables
  + default headway expanded ~50%, contact-aware obstacle inflation, and longer
  stop distance for safer following.
- `selfdrive/controls/lib/latcontrol_torque.py` — Lane change torque ramp now
  preserves the torque present at signal onset across the full 5 s ramp.
- `selfdrive/car/cruise.py` — Experimental mode initializes cruise to current
  speed instead of a fixed 65 mph.
- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` —
  Persistent offset carryover between zones and earlier, stronger gas gating for
  upcoming reductions.

### v2.0.3 Changes

- `selfdrive/controls/lib/longitudinal_planner.py` — Hidden -10% cruise offset now
  applies only when cruise control is the active limiter; lead, SLA, and map
  constraints bypass the offset.
- `sunnypilot/navd/nav_webserver.py` — Web terminal spawns bash with a clean
  (non-venv) environment and branch flashing runs under the same sanitized
  PATH so git fetch/checkout works from the browser UI.

### v2.0.4 Changes

- `selfdrive/controls/lib/longitudinal_planner.py`, `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py`,
  and `sunnypilot/selfdrive/controls/lib/long_v2/*` — Reverted to the v2.0.0
  longitudinal stack pending new tuning.
- `sunnypilot/navd/nav_webserver.py` — Web terminal launches the login shell
  with the venv stripped from the environment and issues an automatic
  `deactivate` to keep Git helpers functional.
- `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` — SCC-V and SCC-M
  badges now show governing MPH values whenever Developer UI is enabled,
  regardless of controller active state.

### v2.0.1 Changes

- `sunnypilot/selfdrive/controls/lib/long_v2/following_v2.py` — Rewritten with closing-rate-aware tier triggering. Tiers 1–4 selected by required deceleration `(v_ego² − v_lead²) / (2·Δd_to_gap)` rather than TTC. Fixes "coast too late" when closing on slow leads.
- `sunnypilot/selfdrive/controls/lib/long_v2/tuning.py` — `decel_comfort` 1.8 → 2.5, `decel_max` 3.5 (new), `jerk_limit_normal` 0.5 → 0.7, `jerk_limit_safety` 3.0 → 4.5.
- `sunnypilot/selfdrive/controls/lib/long_v2/fric.py` — Recalibrated for observed FRIC range 0.08–1.1: nominal dry μ = 0.5, wet/snow boundary at 0.092, comfort/weather scales linearly ramped between.
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_vision_v2.py` / `scc_map_v2.py` — Always publish computed v_target so debug UI can display it when inactive.
- `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` — When `ui_state.developer_ui` is enabled, badges show calculated v_target at all times.
- `selfdrive/controls/lib/longitudinal_planner.py` — Hidden −10% cruise speed offset applied right after `v_cruise = v_cruise_kph * KPH_TO_MS`. UI is unaffected.
- `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` — `COMFORT_BRAKE` 2.0 → 1.5 to enlarge safe-distance and trigger earlier brake initiation.
- `sunnypilot/selfdrive/controls/lib/blinker_pause_lateral.py` — Ported post-blinker unwind from 1.0.8.3. UNWIND_MODE hardcoded to True (avoids params_keys.h whitelist).
- `selfdrive/monitoring/helpers.py` — Restored 9× DM timeouts from 1.0.8.3 (270s passive, 99s active).
- `selfdrive/controls/lib/latcontrol_torque.py` — Blinker-triggered smooth lane change: torque scaled to 25% on blinker rising edge, ramps to 100% over 5.0s. Trigger uses `CS.leftBlinker != CS.rightBlinker` (exactly one blinker on) instead of curvature/angle heuristic.

### v2.0.0 Changes

- `sunnypilot/selfdrive/controls/lib/long_v2/` — New package: physics-based longitudinal control. `tuning.py` externalizes all constants to `Params["LongV2Tuning"]` JSON. `fric.py` reads `liveParameters.frictionCoefficientFiltered`. `jerk_filter.py` is a rate-limited accel filter. `scc_vision_v2.py` detects corners from p97 lateral accel predictions. `scc_map_v2.py` cross-validates mapd speeds against physics and handles corner unwind. `following_v2.py` is a 7-state following machine with predictive TTC and tiered decel. `speed_governor.py` picks the minimum of all v_targets.
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` — Integrated all long_v2 components; `update_targets()` calls v2 controllers and speed governor; publishes `frictionCoefficient`, `weatherCapActive`, `vWeatherCap`, `cornerRadiusAhead` to cereal.
- `selfdrive/controls/lib/longitudinal_planner.py` — Wires in `following_v2.a_override` and `jerk_limit_override` for per-tier decel.
- `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` — New gradient badge renderer: green→orange→red 300ms transition; shows speed in MPH when governing; drop shadow pill shape.
- `cereal/custom.capnp` — Added `frictionCoefficient @8`, `weatherCapActive @9`, `vWeatherCap @10` to `LongitudinalPlanSP`; added `cornerRadiusAhead @6` to `SmartCruiseControl.Map`.
- `sunnypilot/navd/nav_webserver.py` — PTY-backed terminal server on port 8888. `/ws` WebSocket shell, `/api/branches` (date-sorted, newest first), `/api/flash` (branch selector + reboot). Uses asyncio subprocess + PTY fd.
- `sunnypilot/navd/nav_web/index.html` — xterm.js terminal UI with Flash Branch modal showing branches sorted by last commit date.
- `system/manager/process_config.py` — Added `terminal_server` process (always_run).

### v0.9.8 Changes

- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` - Dynamic SLA locking: `_sla_locked` flag + `_dynamic_offset_ratio` float. When SLA is active and user adjusts cruise, records offset % (capped ±50%) and stays locked instead of deactivating. `_effective_speed_limit_target()` applies ratio to new zones. `_update_locked_offset()` recalculates ratio on cruise change with guards (`_speed_limit_final_last > 0`, `v_cruise_cluster > 0`). `_update_confirmed_state()` sets `_sla_locked = True` on activation. Lock cleared only on full disable (`long_enabled = False` or `enabled = False`).
- `cereal/custom.capnp` - Added `slaLocked @5 :Bool` and `slaDynamicOffset @6 :Float32` to `SpeedLimit.Assist` struct.
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` - Publishes `slaLocked` and `slaDynamicOffset` from SLA object.
- `selfdrive/ui/sunnypilot/onroad/speed_limit.py` - Added `_draw_sla_lock_badge()` displaying teal "SLA" badge with offset % (e.g. "+20%") near the speed limit sign when locked. Reads new capnp fields `slaLocked` and `slaDynamicOffset`.
- `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` - Updated follow distance breakpoints to [0, 20, 35, 50, 75] mph with gradients at 20-35 and 50-75mph. Standard: 2.5/1.5/1.0s. Aggressive: 15% lower. Relaxed: 15% higher.

### v0.9.7h Changes (HOTFIX)

- `sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py` - Added empty-array guards before `np.amax()` in `should_cut_gas()` and `np.percentile()` in `_update_calculations()`. On first frame after enabling long control, `modelV2` arrays can be empty, causing `ValueError: zero-size array`. Guards return early if `rate_plan` or `vel_plan` are empty.
- `selfdrive/controls/lib/latcontrol_torque.py` - Moved `import time` from inside hot `update()` loop to module level.

### v0.9.7 Changes

- `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` - Variable follow distance (speed-dependent T_FOLLOW), personality transition gas gating detection, tuned COMFORT_BRAKE/STOP_DISTANCE
- `selfdrive/controls/lib/longitudinal_planner.py` - 70% max accel cap, personality transition gas gating (4s gas gate on dist increase)
- `selfdrive/controls/radard.py` - Lead vehicle smoothing: dRel/vLeadK smoothed for stability, raw aLeadK preserved for fast stoplight response
- `selfdrive/controls/lib/latcontrol_torque.py` - Lane change torque ramp 3.5s/40% start + smooth stop below 15mph
- `selfdrive/monitoring/helpers.py` - Driver monitoring 3x timeouts (90s passive, 33s active)
- `selfdrive/ui/layouts/home.py` - FunnyPilot version display (FunnyPilot X.Y.Z + sp upstream)
- `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` - Gas gating UI indicator (orange badge + "GAS GATE" label)
- `selfdrive/selfdrived/events.py` - speedTooHigh changed to silent static banner (no audio, no disengage, no NO_ENTRY)
- `cereal/custom.capnp` - Added gasGating bool fields to SmartCruiseControl.Vision and .Map structs
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` - Publishes gasGating status from SCC controllers
- `sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py` - SCC-V gas gating + gas_gating_active flag
- `sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py` - SCC-M gas gating + gas_gating_active flag
- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` - Auto-tracks speed limit changes while active; deactivates on manual override; re-prompts on next zone entry; gas gating for limit reductions

### v0.9.6h Changes (HOTFIX)

- `selfdrive/ui/layouts/home.py` - Fixed version display to show both FunnyPilot and upstream sunnypilot versions
- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` - Smart gas gating for speed limit reductions
- `selfdrive/controls/lib/latcontrol_torque.py` - Extended lane change torque ramping from 2.0s to 3.5s, gentler initial torque (40% instead of 50%)

### v0.9.6 Changes

- `selfdrive/controls/lib/latcontrol_torque.py` - Smooth stop (15mph threshold) + lane change torque ramping (50% → 100% over 2s)
- `selfdrive/monitoring/helpers.py` - Driver monitoring (3x original timeouts: 90s passive, 33s active)
- `selfdrive/controls/lib/longitudinal_planner.py` - Max acceleration cap (70% of original values)
- `selfdrive/controls/radard.py` - Lead vehicle smoothing (5-frame moving average for vLead/aLead, 3-frame for dRel)
- `sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py` - SCC-V gas gating (early gas cut at 0.8 lat acc, gentler decel, 3s lookahead)
- `sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py` - SCC-M gas gating (gentler jerk/accel, coast preference, early offset)

### Previous Versions

- `sunnypilot/selfdrive/controls/lib/dec/constants.py` - DEC constants (older versions)
