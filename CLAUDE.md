# FunnyPilot Development Notes

## SSH Access

**Home network (direct):**
```bash
ssh comma@192.168.86.31
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
ssh comma@192.168.86.31 \
  "cd /data/openpilot && git fetch funnypilot && git checkout $BRANCH && git reset --hard funnypilot/$BRANCH && sudo systemctl restart comma"
```

**Then VERIFY — never trust the exit code alone (see v3.4.3 post-mortem):**
```bash
ssh comma@192.168.86.31 \
  "cat /data/openpilot/FUNNYPILOT_VERSION; cd /data/openpilot && git rev-parse --short=9 HEAD; \
   sleep 25; ss -ltn | grep 8888 || echo '8888 DOWN = manager dead'"
```
The version, the hash, AND port 8888 must all be right. A flash that "ran" proves nothing.

**If `git fetch` on the device errors with `Permission denied` on `.git/logs/refs/...`:**
root-owned files inside `.git` (left by a `sudo git` or a root-run flash) block
checkout — `.git/HEAD` itself becomes unwritable, so the `&&` chain dies silently
before `reset --hard`. Fix:
```bash
ssh comma@192.168.86.31 "sudo chown -R comma:comma /data/openpilot/.git"
```
Never run `git` on the device under `sudo`. Also note `cmd | tail` returns *tail's*
exit status — use `${PIPESTATUS[0]}` when checking git through a pipe.

**Push scripts:** Only create a `PUSH<version>.sh` if the user explicitly requests it (e.g. device is offline and a manual-deploy script is needed). Do not create them by default.

## Key Files

- `FUNNYPILOT_VERSION` - Version number only. No changelog.

### v3.5.1 Changes (based on funnypilot-3.5.0e) — STABLE

Onroad refinements after the first drive, plus one real defect.

THE COMMS ALERT WAS OURS, and the rule it leaves behind is the important part:
**nothing in a 20 Hz control-loop process may touch /data.** Not "nothing
slow" — nothing, because on an eMMC shared with loggerd you do not get to know
which call is the slow one. selfdrived marks a service dead after 10 missed
periods (`alive[s] = (now - recv_time) < 10./freq`), which for 20 Hz
`longitudinalPlan` is 500 ms; missing it once raises `commIssue`, a
SOFT_DISABLE, i.e. the full-screen orange "TAKE CONTROL IMMEDIATELY" — and it
clears the moment plannerd catches up. ALARMING + SELF-CLEARING + NO LOSS OF
CONTROL IS THE SIGNATURE OF A LATE FRAME, not of a dead process. v3.5.0's
`maybe_flush()` ran `getsize`/`statvfs`/`makedirs`/append from the planner
loop, at exactly 60 s — the same period as loggerd's segment rotation, so the
two beat against each other and coincided occasionally.

- `sunnypilot/selfdrive/controls/lib/long_v2/scc_learn_store.py` — all writes
  now on a SHORT-LIVED daemon thread handed a finished list of lines. Short-
  lived rather than a worker+queue on purpose: no shared mutable state (no lock,
  no race with the planner mutating `corners`), at most one alive at a time
  (`_writer_busy`), holds one bounded list then dies — the v3.4.6 memory rule.
  `maybe_flush` makes NO SYSCALLS AT ALL; the journal size is a tracked
  `_journal_bytes` counter, and `test_the_flush_path_makes_no_syscalls` pins
  that on the AST because the natural way to write the function is the way that
  caused the bug. `_rewrite` (startup compaction, the biggest write) is
  off-thread too; the READ stays synchronous because the planner needs it.
  `FLUSH_S` 60 -> 47 so nothing is phase-locked to loggerd.
  FALSIFIABLE: `cloudlog.event("commIssue", ...)` names the service in
  `not_alive`. If it is not longitudinalPlan*, this diagnosis is wrong.
- `selfdrive/ui/sunnypilot/onroad/hud/chrome.py` — the state glow is NESTED
  RECTANGLE OUTLINES, not four full-span gradients. The gradients overlapped in
  the corners and composited twice; because the overlap is `depth` square while
  the rails are thousands of px long, that reads as the RAILS fading unevenly
  toward the corners (exactly as reported), not as a bright corner. With rings,
  every pixel belongs to one ring and its alpha is a function of distance to
  the nearest edge — even by construction.
- `selfdrive/ui/sunnypilot/onroad/hud/route_map.py` — jitter fixed at the
  cause: `LastGPSPosition` is 1 Hz, so the projection origin stepped once a
  second and the ribbon snapped with it. Polling faster cannot help (no new
  data). Route is now kept RAW (lat/lon) and re-projected EVERY FRAME from an
  eased pose (`POSE_TAU` 0.35 s, `POSE_SNAP_M` so a relock snaps instead of
  dragging). `bearing_lerp` goes the short way — a wrap past north would
  otherwise spin the route 358°. `edge_fade` replaces the hard `inside()` test
  so travelled road FADES out of the box (there is still no scissor available
  here — see the v3.5.0 nesting rule); `BEHIND_M` keeps 60 m to fade.
- `selfdrive/ui/sunnypilot/onroad/hud/speed_sign.py` — L-shaped column: SLA %
  tab and the upcoming sign now sit to the RIGHT of the main sign, tab on top.
  Station is one sign tall; `width()` added, `height()` is now just SIGN_H.
- `selfdrive/ui/sunnypilot/onroad/hud/stations.py` — `draw_speed` lost the unit
  argument (it never changes on a car, and beside the number it pushed the
  number off-centre by half its width, breaking the centre column's alignment).
  `draw_long_dot` takes a CENTRE now — it lives in the bottom-left corner.
- `selfdrive/ui/mici/onroad/torque_bar.py` — NEW kwargs `opacity`, `grow`,
  `warm_color`, `hot_color`, ALL defaulting to previous behaviour so the mici
  HUD is byte-identical in effect. The fork passes `grow=False` (the height
  ramp grew the bar into the road view exactly when you are looking through it,
  and duplicated what colour already said) and the HUD's own amber/red. Colour
  ramp starts at 0.60 instead of 0.75 since it is now the only channel.
- `selfdrive/ui/onroad/augmented_road_view.py` — driver-monitoring face NOT
  DRAWN under sunnypilot UI. DM is disabled on this fork, so it was a readout
  for a system that cannot act. The renderer is still CONSTRUCTED (keeps
  driverStateV2 flowing); only the draw is skipped.
- `selfdrive/ui/onroad/alert_renderer.py` — informational banners suppressed.
  FILTER IS ON `alertStatus`, NOT on a list of event names: `normal` is
  openpilot's own word for "nothing is wrong", so new upstream events classify
  themselves and nobody has to maintain a list. `AlertSize.full` always shows.
- `FUNNYPILOT_VERSION` -> 3.5.1; EXPECTED_VERSION -> "3.5.1"; four new
  `_CODE_MARKERS` rows.
- TESTS: **506 green.** NEW `bearing_lerp`/`edge_fade` cases and the
  no-syscalls AST guard.

### v3.5.0e Changes (based on funnypilot-3.4.9e)

Onroad UI rewritten on one design system; advisory limits into SCC-M;
SCC-Learn (a corner map built by driving); the experimental-mode wheel button
removed. ZERO schema and ZERO compiled files touched — this branch cannot
trigger a device rebuild.

- `sunnypilot/selfdrive/controls/lib/long_v2/scc_learn_store.py` — NEW,
  stdlib-only. Persistence + geometry index for SCC-Learn. Cell key is
  `(lat_cell, lon_cell, heading_octant)` at `CELL_DEG` ~22 m; THE OCTANT IS IN
  THE KEY because a bend taken northbound and the same tarmac southbound are
  different approaches. A `COARSE_DEG` ~1.1 km index maps to fine keys so a
  lookup probes 9 cells instead of 25,000 records — at 20 Hz that is the whole
  feasibility of the feature. MERGE ON WRITE (`MERGE_M` 30, `MERGE_BEARING_DEG`
  45) and the reason it is load-bearing: a grid has edges, a bend is as likely
  to sit on one as anywhere, and two records with one visit each would be filed
  as two roads-driven-once — VISIT COUNT IS WHAT EVICTION KEEPS, so the merge is
  what stops a boundary corner on the commute being evicted first. Found by a
  test, not by eye. `_evict()` sorts by `(n, t)` reverse: the user's explicit
  requirement, and mutation-tested because sorting by recency looks identical in
  review and is exactly backwards. APPEND-ONLY JOURNAL COMPACTED AT STARTUP —
  plannerd is `only_onroad` so it restarts every drive, which is why NO
  BACKGROUND THREAD IS NEEDED (the v3.4.6 `git gc` OOM rule: bounded in MEMORY,
  not merely in time). Hitting `MAX_JOURNAL_BYTES` mid-drive STOPS WRITING
  rather than compacting; `MAX_JOURNAL_LINES` bounds the startup read
  explicitly. `ALPHA_UP` 0.5 / `ALPHA_DOWN` 0.2 — a cap can only SLOW the car,
  so learning to brake HARDER deserves more evidence. Every operation
  best-effort; losing the file is acceptable, the feature relearns.
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_learn.py` — NEW. `CornerObserver`
  + `SCCLearnV1`. THE RULE THAT MAKES THIS SAFE: commit only on **a local
  minimum in speed FOLLOWED BY RECOVERY, with nothing else explaining it**. The
  recovery requirement is load-bearing and generalizes — a corner is transient,
  while a stop sign, a light and congestion all end in a stop or a long hold, so
  requiring the car to come back UP rejects all three WITHOUT THE SYSTEM NEEDING
  TO KNOW THEY EXIST. Explicit exclusions (each names a non-bend that would
  otherwise be learned): lead at any point, `v_min < MIN_CORNER_V`, posted limit
  changed mid-dip, `sla_busy`, standstill, dip outside `[MIN_DIP_S, MAX_DIP_S]`,
  `gps_acc > MAX_GPS_ACC_M`. The record lands at the APEX not the entry (braking
  is planned TO the corner). Stores `v_min * LEARN_MARGIN` — the minimum already
  contains the driver's/SCC-V's margin, so capping AT it would compound the
  margin every visit until the car crawled. `confidence_for()` 0.45 at one
  visit, 1.0 at three. `read_gps(sm)` prefers the `gpsLocation*` service over
  mapd's `LastGPSPosition` BECAUSE IT CARRIES `horizontalAccuracy` — a record
  keyed on a position we are unsure of is worse than no record — and ages it
  with `sm.recv_time` (the v3.4.5 clock rule); a valid-but-STALE fix is the
  dangerous case, since nothing else rejects it.
  THREE FIXES FOUND IN REVIEW, all measured, all of the same family — a signal
  feeding back into itself: (1) `reset()` MUST clear `_v_ref` on commit, else
  the dip's own exit re-arms the observer and a slow exit commits a duplicate
  record past the apex; (2) `FLAG_SELF` -> `allow_raise=False`, else our cap
  sets v_min, v_min returns +LEARN_MARGIN above it and the estimate ratchets
  2.5%/visit (16.8 -> 22.0 m/s over eleven commutes); (3) `confidence` is HELD
  while `CurveSpeedCap` rides out its release, else the fusion multiplies the
  release ramp by a fresh 0 and deletes the cap in one frame. The observer is
  fed `carState.vEgo`, NOT the planner's `v_ego` (`v_desired_filter.x` is the
  plan integrated forward, pinned to vEgo only while disengaged — learning off
  it would use a different signal depending on engagement).
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_fusion.py` — NEW
  `fuse_learned_target()`, `LEARN_SOLO_MAX_CUT = 8.9`, `LEARN_MIN_CUT = 0.5`.
  DELIBERATELY NOT a second call to `fuse_map_target`: the vision veto exists
  because OSM's curve speeds are COMPUTED BY SOMEONE ELSE, whereas a learned
  point is a speed THIS CAR ACTUALLY WENT THROUGH THIS BEND after the observer
  rejected everything that was not a bend. It is self-corroborating in the exact
  sense the map is not. What replaces the veto is VISIT COUNT; vision agreeing
  may only RAISE authority (`max()`, mutation-tested — assigning looks the same
  in review).
- `sunnypilot/selfdrive/controls/lib/long_v2/speed_governor.py` — `v_scc_learn`
  is its OWN candidate, not folded into `scc_map`: its authority comes from
  visit count, not OSM, and the two must be able to disagree without one
  masking the other.
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` (SP) —
  `_update_scc_learn()` gathers the exclusion signals and is TOTAL (any failure
  degrades to no learned data). Learn happens BEFORE the cap is read so a corner
  is never capped from the pass that is recording it. Reports as
  `LongitudinalPlanSource.sccMap` — the capnp enum has no room for a new member
  and a schema change forces a rebuild.
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_shm.py` — NEW
  `/dev/shm/fp_learn` (`write_learn_shm` / `read_learn_shm`). A SEPARATE FILE
  from `fp_scc` on purpose: that reader unpacks positionally and its contract is
  pinned by tests, and widening a working channel for an unrelated feature is
  how a reader that indexes `[4]` starts reading a different quantity.
- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` — NEW
  read-only `busy` property (mid-ramp or gas-gating). Without it there would be
  a permanent learned corner cap at every speed-limit sign on the commute.
- `selfdrive/ui/sunnypilot/onroad/hud_renderer.py` — "LRN" pill: lit while a
  learned corner governs, else the known-corner count, so an empty store reads
  as "nothing learned yet" rather than as a broken feature.
- TESTS: `long_v2/tests/test_scc_learn.py` (73). TEN SCC-Learn guards
  MUTATION-TESTED: eviction by recency, symmetric alpha, no cell-edge merge,
  committing without recovery (5 failures — the one that turns every red light
  into a corner), lead not poisoning a dip, one visit trusted like ten, corners
  behind us still capping, corroboration overriding confidence, unbounded cut,
  a self-governed pass raising the estimate.

- `sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py` — reads
  `MapAdvisoryLimit` / `NextMapAdvisoryLimit`, unread since mapd shipped. THE
  DISTINCTION THAT JUSTIFIES IT: `MapTargetVelocities` is geometry mapd
  COMPUTED; an advisory limit is a speed an engineer SURVEYED AND SIGNED, i.e.
  the best available answer to "is this corner real" — the exact question the
  vision veto asks. Used ONLY as (1) a floor on the cap, bounded by
  `ADVISORY_MARGIN` 1.15 and `ADVISORY_MAX_CUT` ~20 mph, `min()` only so it can
  never raise a geometry cap, AND ONLY WHERE GEOMETRY IS ALREADY CONSTRAINING
  (an advisory tag is per-WAY, so an unguarded `min()` holds the car down along
  every straight between a curvy road's bends — the guard is the fix, the
  unguarded version reads as obviously correct); and (2) corroboration. NOT a target: advisory
  tags are per-WAY, so obeying one literally holds the car down through every
  straight between a curvy road's bends. `_advisory_cap` returns CAP_INACTIVE
  on any doubt, making every consumer a no-op. Also records `gov_lat/gov_lon`,
  the point `argmin(v_allowed)` chose.
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_fusion.py` —
  `ADVISORY_CORROB_FLOOR = 0.6`. GENERALIZED: **a confirmation gate should
  accept independent evidence, not only the one sensor it was written around.**
  It is a FLOOR (`max(c, FLOOR)`), never an override — where vision already
  corroborates fully the advisory changes nothing, and `MAP_SOLO_MAX_CUT` still
  bounds the result. A test pins that distinction because assigning instead of
  max()-ing looks identical in review.
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_shm.py` — NEW. plannerd
  publishes the governing point + how much of its cut survived the fusion. The
  UI draws that rather than re-deriving `argmin`: **two copies of a selection
  rule drift the moment either is tuned, and a debug readout that disagrees
  with the controller is worse than no readout.** Same /dev/shm pattern and
  staleness contract as sla_shm (wedged publisher reads as "no constraint").
- `selfdrive/ui/sunnypilot/onroad/hud/` — NEW package: `tokens.py` (every
  visual constant + `safe_draw`), `chrome.py` (vignette, state glow, bands),
  `speed_sign.py`, `route_map.py`, `stations.py`.
  * STATE GLOW replaces the solid ring: 60% at the frame edge, linear to zero
    over 120 px, four gradient rects. THE VIGNETTE UNDER IT IS LOAD-BEARING —
    a glow on a bright sky washes out completely, which would make the
    indicator useless in exactly the conditions you want it. Order is the
    trick: vignette (220 px, 72%), glow, bands, content.
  * HORIZON BANDS: 350 px top scrim, 138 px bottom, middle third never drawn
    into. EVERY STATION RESERVES ITS SPACE — the dev rail's
    `gap_width = (available - total)/gaps` over a conditionally built list
    meant one dropped `liveDelay` slid every metric sideways. Nothing reflows.
  * THE SIGN CARRIES EVERY SLA STATE WITH NO TEXT: halo colour = direction the
    limit is moving, halo thickness/bloom = how close (hairline at 400 m ->
    solid ring at the boundary). `halo_spec()` is pure and unit-tested. MUTCD
    and Vienna faces UNCHANGED — legally recognisable iconography.
  * ROUTE MINIMAP: `MapTargetVelocities` ego-centric, tinted by
    `posted_limit - map_target_velocity` (neutral -> amber -> red across
    3..25 mph under). NO SIDE ROADS AND THAT IS A FINDING: mapd publishes 11
    params and none contain junctions. Drawing more would show geometry the
    controller cannot see.
- `selfdrive/ui/sunnypilot/onroad/hud_renderer.py` — REWRITTEN. Does NOT call
  `HudRenderer._render` (the base still owns `_update_state`). WHEEL BUTTON
  GONE: experimental mode comes solely from the offroad setting; behaviour is
  unchanged because selfdrived always published it. E2E pill keeps it visible.
- `selfdrive/ui/onroad/augmented_road_view.py` — `_draw_edge_treatment()`
  between the model and the HUD; `_draw_border` skips the coloured ring under
  sunnypilot UI.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py` —
  geometry EXPORTED (`RIGHT_COL_WIDTH`/`RIGHT_COL_MARGIN`) but otherwise
  UNCHANGED. A draft pushed this column below the minimap; on the 1020 px
  content area that gave five 120 px-tall elements a 104 px pitch, so they
  overlapped each other, hit the bottom rail and the last fell off-screen. The
  MINIMAP MOVED SIDEWAYS INSTEAD (`MAP_RIGHT_INSET`, keyed off the dev-UI
  constants so the two cannot drift). Check any onroad geometry change with
  arithmetic, not by eye — none of it can be rendered off-device.
- DELETED (zero importers after the rewrite): onroad `smart_cruise_control.py`,
  `speed_renderer.py`, `rocket_fuel.py`.

NOT-CRASHING-THE-DEVICE RULES ESTABLISHED HERE — read before touching any
onroad UI file. `selfdrive/ui/ui.py` draws BOTH screens, so a raise onroad
kills the UI, manager restarts it, it dies again = boot loop on a device whose
settings screen is how you would flash out of it.
  * `tokens.safe_draw` wraps every new widget: first exception logs and
    disables THAT widget for the session. No retry, no re-enable — a widget
    raising at 60 Hz burns the frame budget the rest of the UI needs.
  * Nothing in `hud/` may do IO at import or in a constructor. RouteMap's
    Params handle is lazy because /dev/shm/params does not exist offroad.
    `test_hud_imports.py` pins this on the AST.
  * NO NEW PARAMS EVER without checking: registering one edits
    `common/params_keys.h`, which is C++ and compiles.
  * NO new assets, no shaders, no schema changes. Every raylib call shape used
    is one that already appears elsewhere in this repo.
  * DO NOT nest `begin_scissor_mode` inside AugmentedRoadView's. raylib's
    `EndScissorMode` DISABLES the test, it does not restore an outer region —
    nesting silently un-clips every widget drawn after it that frame. The
    minimap drops out-of-box segments instead.
  * `draw_triangle_fan` takes plain (x, y) tuples here (see
    onroad/model_renderer.py). Prefer it to `draw_triangle`, which is
    winding-order sensitive and silently draws nothing when wrong.
- TESTS: **497 green, 0 failed.** NEW `test_hud_imports.py` (15),
  `test_hud_logic.py` (45), `test_scc_advisory.py` (24),
  `test_scc_learn.py` (73). Seven UI/advisory guards
  MUTATION-TESTED: advisory raising instead of lowering, unbounded advisory
  cut, advisory overriding rather than flooring corroboration, scc_shm
  staleness, safe_draw not disabling, the minimap rotation with sin/cos swapped
  (mirrors every corner and looks plausible), a halo that stops tracking
  distance.
- ON-DEVICE VERIFICATION REQUIRED: no drawing can be tested off-device (no GL
  context, no camera). Logic is unit-tested and the import path is guarded; the
  LOOK has to be judged on the car. If a widget vanishes on-road, grep the log
  for "onroad hud widget ... disabled" — that names the failure exactly.

### v3.4.9e Changes (based on funnypilot-3.4.8)

Four requested behaviour changes plus a dead-code sweep. Read the KnotFilter
entry first — it is the one that finally makes the lagd window pay for
smoothness without paying in phase.

- `selfdrive/controls/lib/knot_filter.py` — NEW, stdlib-only. `lat_smooth.py`
  can only shape the path BETWEEN 20 Hz knots; the knot SEQUENCE still handed
  every rate change over whole inside one 50 ms period, which is the step the
  driver feels. THE RULE THIS ENCODES, and the reason it is not the v3.2.12 EMA
  that had to be reverted: **an EMA's process model is "the curvature stays put",
  so it lags EVERYTHING, including motion the model had already announced.**
  KnotFilter's process model is the MODEL'S OWN PUBLISHED PLAN. controlsd
  already samples the plan one model step past the action horizon; because the
  action is the plan at `lat_delay + DT_MDL` and the lookahead is the same plan
  at `lat_delay + 2*DT_MDL`, that sample IS a prediction of the next frame's
  action. Only the INNOVATION (raw minus prediction) is damped. Measured, not
  claimed: perfect prediction is BIT-IDENTICAL passthrough; an unpredicted step
  at the model's own rate rail is spread 45/32/13/6/2/1% over six frames (peak
  frame-to-frame command change -> 45%); jitter -> 52%; the degenerate blind
  case settles 42 ms behind, UNDER ONE MODEL FRAME. `DEV_MAX_LAT_ACCEL = 0.15`
  hard-caps |command - desire| in m/s^2 (~3 mm of path); max REACHABLE deviation
  is 0.135, so the cap is a backstop for a wrong prediction, not the operating
  point. Large surprises get beta == 1 = untouched (the v3.3.2 "onsets stay
  decisive" requirement).
- `selfdrive/controls/controlsd.py` — KnotFilter wired between
  `action.desiredCurvature` and `LatSmoother.update`; `set_prediction(next_est)`
  after each knot. NEW `_model_action_delay()`: `_model_lookahead_curv` measured
  its horizon from `liveDelay.lateralDelay`, but with the lagd toggle ON
  modeld_v2 places the ACTION at the CACHED lagd value, so the "lookahead" could
  land BEHIND the action and hand the v3.3.6 spline a REVERSED exit slope
  (bounded by the Fritsch-Carlson clamp, but wrong). It now reads the same
  `get_lat_delay` answer ControlsExt computes, with a getattr fallback (the attr
  is only set for torque tunes, and not before the first `get_params_sp`).
  `/dev/shm/lat_interp` gained a FIFTH field, the filter's deviation in m/s^2
  (appended; the dev-UI reader indexes defensively so old readers are fine).
- `selfdrive/controls/lib/lat_handback.py` — NEW, stdlib-only. THE REPORTED
  LIMIT CYCLE: driver holds the wheel out of the model's line -> softening cuts
  torque to 60% -> driver settles the car and relaxes -> softening releases in
  ~0.15 s with the model's desire UNCHANGED and a frozen integrator still
  holding pre-override wind-up -> bite -> grab again, once per corner.
  GENERALIZED LESSON: **v3.2.8's OverrideGate fixed the ENGAGE side (chatter)
  and nobody looked at the RELEASE side.** A dwell hysteresis says WHEN to hand
  back; it says nothing about HOW FAST, and the release was the same near-step
  whether the two desires were 0.1 or 3 m/s^2 apart. The return is now a
  smoothstep ramp whose duration interpolates on the desired-vs-measured
  lateral-accel DIVERGENCE: `T_SOFT` 1.6 s when close (the corner case — nothing
  to correct, so no reason to snatch) to `T_FIRM` 0.45 s when far (evasive).
  Divergence is PEAK-HELD with a bleed across the press, deliberately: in the
  reported scenario the driver has ALIGNED the car by the time they relax, so an
  instantaneous sample reads ~0 and would schedule the wrong ramp. OverrideGate
  is reused verbatim as the dwell primitive.
- `selfdrive/controls/lib/latcontrol_torque.py` — `_handback` replaces
  `_override_gate` + `_override_filter` and owns the whole scale; computed at
  the TOP of the active branch because it also gates the integrator.
  `INTEGRATOR_BLEED_TAU = 1.0`: while the driver is actually in charge the
  integrator is BLED, not merely frozen — a frozen one still holds the
  pre-override wind-up, and dumping that back in is the other half of the bite.
  `soft_integrator` keeps it frozen through the first half of the return ramp.
  `_OVERRIDE_MIN_SCALE` is kept as an alias of `lat_handback.PRESS_SCALE` (the
  nav_webserver marker greps it).
- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` — TWO
  SLA defects. (1) `get_v_target_from_control` returned the CURRENT ZONE's
  target. That value is SLA's entry in the speed governor's `min()`, so on an
  approach to a FASTER zone the ramp walked the cluster up exactly as designed
  and this one line threw the result away — `min(rising cluster, old zone
  target)` is the old zone target, and the whole rise then arrived as a step at
  the boundary. GENERALIZED: **a feature that publishes a ramp must publish the
  RAMPED value to every consumer, not just the visible one.** The descent never
  showed it because there the cluster is the more restrictive of the two, so
  `min()` picked the ramp's value by accident. (2) NEW
  `_apply_driver_set_speed_change` + `RAMP_DISPLACED_TH`: a cruise press DURING
  a ramp was read as an ABSOLUTE statement about the current zone, but the ramp
  is what put the cluster there — +30% in a 50 zone walked down to 40 became
  -18% on one tap, and the next zone was entered ~20 mph low ("it forgets where
  SLA was set"). A press while DISPLACED now moves the ratio by what the driver
  added on top of the ramp; parked on the zone target the absolute derivation
  still runs (that is what enforces the +/-50% RATIO_LIMIT rail). The press also
  no longer ABORTS the descent — `_latch` shifts by the same delta and
  `_d_min`/`_confirm_n`/`_engage_grace` survive.
- `sunnypilot/selfdrive/car/cruise_ext.py` — `_ramp_hold_frames` 100 -> 60. It
  has to expire at roughly the same time as SLA's own 0.5 s intent window; at
  1 s, SLA's ramp ran for half a second while this side still refused to follow
  it, so the cluster JUMPED when the hold expired. A test pins the relationship
  (not the number).
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_fusion.py` — NEW. The v3.3.8
  binary veto asked "has SCC-V ACTIVATED", not "does the model see a corner".
  GENERALIZED: **a confirmation gate must be defined on the EVIDENCE, not on
  another controller's ACTION threshold.** The map reasons to 400 m and the
  plan reaches ~240 m at 30 m/s, so the veto was hardest exactly where the map's
  early gentle reduction was most useful. Corroboration is now CONTINUOUS and
  SCALES the map's authority, bounded by `MAP_SOLO_MAX_CUT` (~15 mph); a
  straight road still vetoes outright, so the bad-map-data protection is fully
  intact. `speed_governor.gate_map_target` is a thin alias (old 2-arg call shape
  still works and still vetoes).
- `sunnypilot/selfdrive/controls/lib/long_v2/scc_vision_v2.py` — THE SELECTION
  MASK WAS ASKING THE MODEL'S OPINION OF ITS OWN PLAN. A point counted as a
  corner only when `orientationRate.z * velocity.x` exceeded the comfort limit,
  but `velocity.x` is what the model INTENDS to do there and the model plans to
  slow for corners — so a real corner read as "nothing to do", while in `acc`
  mode the car never follows that planned velocity. The corner SPEED never had
  the bug (`v_i*sqrt(a/(rate*v_i))` is algebraically `sqrt(a/curvature)`); only
  the mask did. Points now bind on corner speed < `max(v_ego, v_cruise)`. The
  cruise term is LOAD-BEARING: with v_ego alone the mask empties the moment the
  car has slowed TO the corner speed, releasing the cap and oscillating inside
  the corner. Also publishes `corroboration` for scc_fusion. `_MAX_HORIZON_T`
  7 -> 8 s.
- `sunnypilot/selfdrive/controls/lib/long_v2/tuning.py` — `a_lat_target`
  2.4 -> 2.1 m/s^2, and the dataclass is now EXACTLY the fields control code
  reads (`a_lat_target`, `sccm_speed_trim`, `road_type_caps`).
- DEAD CODE SWEEP (v3.4.9e). All of it verified unreferenced by AST scan before
  deletion, not by eye:
  * `sunnypilot/selfdrive/controls/lib/smart_cruise_control/` DELETED (6 files,
    841 lines) — the legacy v1 SCC package, superseded by `long_v2/` in v3.2.6e
    when the planner stopped importing it. Zero importers since. NOTE the UI
    file `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` is a DIFFERENT
    file and is very much alive; do not confuse the two.
  * `long_v2/jerk_filter.py` DELETED — its only consumer was `following_v2.py`,
    deleted in v3.2.6e.
  * `long_v2/tests/test_physics.py` DELETED — it re-defined `k*sqrt(fric*g)`
    corner formulas LOCALLY and asserted on them, so it tested nothing in the
    codebase and had been failing (17 cases) since v3.2.6e replaced those
    formulas. WATCH FOR THIS SHAPE: a test that defines its own copy of the
    maths cannot fail when the real maths changes, only when the copy drifts.
  * seven dead `LongV2Tuning` fields, `fric.comfort_scale`,
    `tuning.reset_tuning_cache`, `elements.LeadSpeedElement`.
  * `_BadgeState` in the SCC UI had TWO `__init__` definitions. The second won
    (as always), and the second is the CORRECT one — the first never set
    `_from`, which `tick()` reads after any `set_target()`. Deleting the first
    is a runtime no-op; "fixing" the duplication the other way would have
    shipped an AttributeError into the onroad UI.
  * ruff is now CLEAN across `selfdrive/ sunnypilot/ system/ common/` (was 13
    errors). Keep it there — a zero baseline is the only one where a new warning
    means anything.
- `FUNNYPILOT_VERSION` -> 3.4.9; nav_webserver `EXPECTED_VERSION` -> "3.4.9",
  two new `_FEEL_FILES` rows, eight new `_CODE_MARKERS` rows.
- TESTS: **348 green, 0 failed** (was 197 green + 20 failed). NEW
  `test_knot_filter.py` (16), `test_lat_handback.py` (16); SCC/SLA/cruise_ext
  suites extended. Seven guards MUTATION-TESTED fail-then-restore: publishing
  the zone target, absolute ratio re-derivation mid-ramp, masking on the
  planned velocity, binary corroboration, unbounded solo cut, damping the raw
  action (becoming an EMA), a fixed release time constant, the 1 s ramp hold.
- ON-ROAD VERIFICATION REQUIRED / FALSIFIABLE: (1) if the lateral still feels
  under-damped, the honest next lever is `N_FULL_LAT_ACCEL` / `CARRY` in
  knot_filter.py — NOT a filter on past outputs, ever. (2) if the handback still
  bites, read the divergence schedule before retuning: a bite with a SMALL gap
  means the ramp is not the mechanism and the next suspect is the friction relay
  (see the v3.3.8 BumpDamper notes). (3) if SCC now slows for things it should
  not, `a_lat_target` and `CORROB_FRAC` are the two knobs; a map-only slowdown
  on a straight road would mean the corroboration veto has broken and is a BUG,
  not a tuning issue.

### v3.4.8 Changes (based on funnypilot-3.4.7)

Three defects in the v3.4.5 ramp, all from ONE reported drive into a HIGHER
zone. Read the gas gate one first — it is the only one that could leave the car
unable to accelerate on a highway.

THE GATE BUG, generalized: **a gate defined on a COMMAND may not be applied
without checking the STATE it is supposed to protect.** `gas_gate_active` was
`v_cruise_target < effective_speed_limit_target` — pure set-speed geometry. The
gate exists so the car does not add throttle to FIGHT the ramp; when v_ego is
already at or below the ramp target there is no fight, but it fired anyway and
pinned `accel_clip[1]` to the coast accel. Symptom: entered a zone below target,
set speed snapped up, car then coasted ~10 s; the pedal did not help (the gate
is still there when the override releases) and only cycling SLA cleared it.
v3.4.7's ~490 m envelope made the trigger condition (a lower zone within the
envelope right after entering a zone below target) common.

- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` —
  `_update_gas_gate()` rewritten with three narrowings, ALL fail-safe (the gate
  can now only engage in strictly fewer situations): (1) `GATE_V_MARGIN = 0.5`
  m/s — v_ego must actually be ABOVE the ramp target; (2) compares against
  `_clamp_set_speed(effective_speed_limit_target)` — `v_cruise_target` is
  clamped and the right-hand side was NOT, so any target above
  `V_CRUISE_MAX_KPH` (a high limit with a carried positive ratio) or below the
  min set speed made `clamped < unclamped` true FOREVER = a latched gate with no
  exit; (3) `GATE_MAX_FRAMES = int(30.0 / DT_MDL)` watchdog — a WATCHDOG, not a
  tuning knob; longest legitimate hold is one descent (~15.6 s).
  UP-RAMP, a UNIT ERROR: `RAMP_UP_DIST = 90.0` m was a DISTANCE window on an
  output bounded by a RATE (`RATE_MAX * DT_MDL`), so it truncated whenever
  `dv > RATE_MAX * (d / v_ego)` — at 55 mph, 90 m = 3.66 s = 9.8 mph of a 15 mph
  rise, i.e. the reported "arrived in the new zone below target, then it snapped
  up". DELETED; replaced by `RAMP_UP_T = 5.0`, `RAMP_UP_T_MAX = 8.0`,
  `RAMP_UP_D_MIN = 40.0` — the window is a TRAVEL TIME sized from the rise
  (`t = dv / RATE_NOM`), so it means the same thing at any speed.
  RAMP_UP_T_MAX STILL TRUNCATES LARGE RISES ON PURPOSE: early on the DOWN side
  is free, early on the UP side is speeding early, and the boundary re-seed
  already covers the remainder.
  ENGAGEMENT HYSTERESIS: `CONFIRM_N` guarded ENTRY, nothing guarded
  CONTINUATION. One dropped mapd frame reset `_confirm_n` to 1 and the ramp fell
  through to `target = current_target`, driving the set speed the WRONG way for
  >= CONFIRM_N frames — the reported "flickered down a mph". `liveMapDataSP` is
  1 Hz, the route match blinks, and `d` reaches 0 before the current limit
  flips, so this is routine, not exotic. A confirmed zone is now carried through
  a dropout by DEAD RECKONING (`_engage_target`/`_engage_d`, d closing at
  v_ego), bounded to 1 s by `ENGAGE_GRACE_FRAMES`. Bounded deliberately: a zone
  that genuinely vanished must not hold the set speed hostage.
- `sunnypilot/selfdrive/controls/lib/speed_limit/tests/test_sla_cruise_ramp.py`
  — `TestGateNeverStrandsTheCar` (3) and `TestEngagementSurvivesDropouts` (1)
  are the direct regression guards; `up_window()` helper replaces the deleted
  RAMP_UP_DIST in the up-ramp tests. NOTE the pre-existing gate tests ran only
  `CONFIRM_N + 6` frames, which is no longer enough for v_ego to fall behind the
  target — they now run to 60. WATCH FOR THIS SHAPE: a gate test that never lets
  the plant move cannot see a v_ego term.
- `FUNNYPILOT_VERSION` -> 3.4.8. **v3.4.7 changed the ramp geometry and never
  bumped this file**, so a device on funnypilot-3.4.7 reported 3.4.6 — check it
  when a version readout disagrees with the branch.
- `sunnypilot/navd/nav_webserver.py` — `EXPECTED_VERSION` -> "3.4.8"; four new
  `_CODE_MARKERS` rows (`GATE_V_MARGIN`, `GATE_MAX_FRAMES`,
  `ENGAGE_GRACE_FRAMES`, `RAMP_UP_T_MAX`).
- All three guards MUTATION-TESTED (fail-then-restore). 201 green across the
  speed-limit, car and controls import-light suites. TESTING NOTE: the suites DO
  run in a bare container with `--noconftest` (conftest imports `params_pyx`)
  plus pip installs of pytest/numpy/pycapnp/setproctitle/zstandard; the 3
  `test_triage_recorder.py::TestWebserverHelpers` failures are a missing
  `aiohttp` only.
- ON-ROAD VERIFICATION REQUIRED: the ~10 s coast is diagnosed from code, not
  from a log (the device was not connected). If it recurs with the gate fix in,
  the gate is exonerated and the next suspects are the planner's `accel_clip`
  rate limiter (0.05/frame, and `prev_accel_clip` is NOT reset on `reset_state`)
  and `LeadGrace`. Do NOT retune the ramp constants for it.

### v3.4.7 Changes (based on funnypilot-3.4.6)

Widened the predictive ramp's runway: `RAMP_ARRIVE_EARLY_T` 1 -> 3 s (the set
speed must FINISH before the sign, because the car trails the set speed —
landing the number ON the boundary means still decelerating through it),
`RAMP_D_MAX` 250 -> 400 m, `RAMP_T_MAX` 15 -> 20 s. 70 -> 45 mph engages
~494 m / 15.6 s out, was ~281 m / 9.7 s. `RATE_NOM` untouched (1 mph/s was the
explicit request; it still governs gentle changes, which just start sooner).
NOTE: this branch did NOT bump `FUNNYPILOT_VERSION` — fixed in 3.4.8.

### v3.4.5 Changes (based on funnypilot-3.4.4)

SLA plans for the sign instead of reacting to it. But the FEATURE is not the
important part of this release — the CLOCK-DOMAIN FIX is. Without it, the
v3.4.0 ramp and the v3.3.3 pre-zone gas gate were both inert on a moving car
while every readout said they were fine.

ROOT CAUSE (read this before touching anything upcoming-zone related):
`speed_limit_resolver.py` aged map data with

    time.monotonic() - sm['gpsLocation'].unixTimestampMillis * 1e-3

i.e. a UNIX EPOCH (~1.8e9) subtracted from a SINCE-BOOT counter (~1e4).
Measured on this device: about **-1.785e9 s**, making
`distance_to_next_limit` about **3.93e10 m** at 22 m/s. Consequences, both
silent: the down-ramp envelope evaluated to ~2.5e5 m/s so `min(target,
envelope)` never bound; the gas gate never fired above standstill; the
up-ramp blend clipped to 0. What the driver actually felt at a zone boundary
was ONLY the old `RAMP_MAX_RATE = 4.0` slew unwinding at ~8.95 mph/s — the
"it spams one mph at a time once we're already in the zone" report.
The staleness gate was broken in the same expression: `age >
LIMIT_MAX_MAP_DATA_AGE` could never fire for a real fix, so it was an
accidental *has-ever-had-a-GPS-fix* test wearing a freshness test's clothes.

CLOCK RULE, generalized: **`sm.recv_time[service]` is the only age source
that is domain-safe.** SubMaster stamps it with the CONSUMER's
`time.monotonic()` no matter who published. `logMonoTime` is NOT a
substitute — `cereal/messaging/__init__.py:45` stamps it with
`time.monotonic()` for Python publishers while C++ ones use `CLOCK_BOOTTIME`
(`common/timing.h`), so it reintroduces this bug class through another door.
`recv_time` is `0.` until the first message, which is a *reject*, not a
zero age. Corollary elsewhere: where the comparand is a WALL-CLOCK value
(`os.stat().st_mtime`), monotonic is the wrong clock — see brake_light_shm.

- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_resolver.py` —
  `MAP_MSG_MAX_AGE = 2.0` (> 1 s so a single dropped message on the 1 Hz
  `liveMapDataSP` doesn't blink the upcoming zone out). `_process_map_data`
  gates on `sm.valid['liveMapDataSP']` (which is literally `llk.gpsOK`, see
  `base_map_data.py:50` — the GPS-fix condition the epoch subtraction was
  groping for) and computes `map_age = max(0., time.monotonic() -
  sm.recv_time['liveMapDataSP'])`. `_calculate_map_data_limits` now takes
  `map_age` as a PARAMETER rather than recomputing it: the two must not be
  able to disagree. `unixTimestampMillis` and `LIMIT_MAX_MAP_DATA_AGE` are
  gone and a test asserts they stay gone. ~20 lines of comment record the
  measured numbers above.
- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` —
  `_update_cruise_ramp()` rewritten with a full math derivation in its
  docstring. DISTANCE-PARAMETERISED constant-decel envelope
  `v_set(d) = sqrt(v_next^2 + 2*a*d_eff)`. Distance, not a timer: no `v_ego`
  division (standstill-safe) and it is SELF-CORRECTING — every frame
  re-solves from the current distance, so a late-appearing zone, a route
  change, or the driver slowing early all converge with no state to get
  stuck in. New constants: `RATE_NOM = 0.45` m/s per second (~1 mph/s, the
  user's "1 mph every 1 second"), `RATE_MAX = 1.2`, `RAMP_T_MAX = 15.0` s,
  `RAMP_D_MAX = 250.0` m, `RAMP_ARRIVE_EARLY_T = 1.0` s (target reached
  BEFORE the sign, not at it), `RAMP_UP_DIST = 90.0`, `CONFIRM_N = 3`,
  `LATCH_RELEASE_D = 30.0`, `BUTTON_INTENT_FRAMES = int(0.5 / DT_MDL)`,
  `MIN_SET_SPEED_KPH_METRIC/IMPERIAL`, `V_CRUISE_MAX_KPH = 145`.
  `RATE_MAX` IS NOT A TASTE VALUE: `CRUISE_MIN_ACCEL = -1.2`
  (`long_mpc.py:65`) is a structural ceiling on what a falling cruise speed
  can command, so any faster slew is display-only theatre. Do not raise it
  without changing that first.
  DELETED: `RAMP_DECEL`, `RAMP_MAX_RATE`, `GATE_COAST_ACCEL`,
  `GATE_TIME_BUFFER`, `GATE_MIN_OVER`, `_cluster_change_is_ours`,
  `_commanded_conv`, `_prev_commanded_conv`. The gas gate is now defined off
  the ramp's own envelope instead of a second independent coast model.
  RATIO RE-DERIVATION IS NOW BUTTON-GATED (`if self.v_cruise_cluster_changed
  and self._button_event_recent():`). Mid-approach the cluster sits BETWEEN
  zones, so re-reading the carried offset from a cluster change the RAMP
  caused would silently collapse a +20% into whatever the ramp was passing
  through. v3.4.0's one-frame `_cluster_change_is_ours` could not tell the
  two apart once the ramp spanned many frames.
- `sunnypilot/selfdrive/car/cruise_ext.py` — `update_speed_limit_assist_v_
  cruise_non_pcm` is now a SINGLE WRITER of `self.v_cruise_kph`. It used to
  have two unchained `if` blocks — the documented "idempotent boundary snap"
  and the ramp follow — with the ramp LAST, so the snap was overwritten
  before it reached the car. Dead code that read like the authoritative
  path. Writes land on the DISPLAY GRID (`IMPERIAL_INCREMENT =
  round(CV.MPH_TO_KPH, 1)`, 1 kph metric via `self.sla_is_metric`): the same
  lattice the cruise buttons produce, so the ramp reads on the cluster as
  ordinary 1-mph taps AND the driver's next `+` press cannot snap to a grid
  point and silently eat part of the change. The "CS is deliberately
  UNANNOTATED" comment block (the v3.4.2 boot fix) is untouched.
- `sunnypilot/selfdrive/controls/lib/speed_limit/sla_shm.py` — payload is now
  3 fields, `"<target>,<gate>,<time.monotonic()>"`, with `STALE_S = 0.5`.
  cruise_ext writes this channel's value into `v_cruise_kph` at 100 Hz, so
  an unstamped channel meant a wedged plannerd left its last target in the
  file forever and the car kept obeying a dead process — including reverting
  the driver's own SET+ press a fraction of a second after they made it.
  THE FAILURE MODE OF THIS CHANNEL MUST BE "NO REQUEST", NEVER A STUCK ONE.
  A legacy 2-field line reads as stale, never as trusted.
- `system/manager/storage_cleanup.py` — stdlib-only (an openpilot import here
  would drag cereal/params onto the boot path before manager has set them
  up). Called from `manager_init()` via `cleanup_async()` on a background
  thread — NEVER synchronously, which would add the size walk to every boot.
  It is an ALLOW-LIST, not a walk-and-decide: absolute paths, no globs, and a
  test asserts `/`, `/data`, `/data/openpilot`, `/data/params`,
  `/data/media` can never appear in it. `cleanup()` is TOTAL — no input makes
  it raise, because it runs before the car can start.
  v3.4.6 — NO SUBPROCESSES. The `_git_gc()` step is GONE and must not return;
  it was the v3.4.5 memory leak (measured 1.69 GB peak RSS, see the changelog
  and the module docstring). The rule this leaves behind is the important
  part: this module runs beside a moving car on a device with no swap, so
  every step must be bounded in MEMORY, not merely in time. A
  `subprocess.run` timeout is not a memory bound — it kills only the direct
  child, and it was `git`'s `pack-objects` grandchildren doing the
  allocating. `TestNoSubprocesses` pins this on the AST.
  Thresholds (`LOW_BYTES`, `LOW_PERCENT`) sit ABOVE `deleter.py`'s 5 GB/10%
  floor so this engages BEFORE drive logs get eaten, not after. READ THE
  CONSEQUENCE, which v3.4.5 missed: deleter holds free space AT its floor, so
  `low` is true on essentially every boot. `low` does not mean "rare", and
  only cheap rmtrees may hang off it.
  The actual cause of the user's "storage full" message was never diagnosed
  (device offline at the time) — the `storage` Verify row exists so the next
  occurrence produces evidence.
- `sunnypilot/navd/nav_webserver.py` — `EXPECTED_VERSION` -> "3.4.6"; new
  `/api/version` endpoint + `_VERSION_FILE`; new `storage` DIAG row (`df`,
  graded on use%); four new `_CODE_MARKERS` rows (`MAP_MSG_MAX_AGE`,
  `RAMP_ARRIVE_EARLY_T`, `BUTTON_INTENT_FRAMES`, `STALE_S`), 36 total.
- `sunnypilot/navd/nav_web/index.html` — version pill in the title bar.
- TESTS — 291 green off-device (was 200). NEW:
  `speed_limit/tests/test_speed_limit_resolver_clock.py` (11) drives the
  PRODUCER; the reason nothing caught the epoch bug for two years of forks
  is that every SLA test fed `next_distance` in by hand and so exercised the
  consumers with known-good metres. `speed_limit/tests/test_sla_shm.py` (19)
  pins the freshness contract — the stale cases deliberately leave a VALID
  non-zero target in the file so only the timestamp is old.
  `car/tests/test_cruise_ext_sla_ramp.py` (10) pins quantization and, via
  AST, the single-writer invariant (AST not grep: the method's comment block
  describes the deleted writer at length). `system/manager/tests/
  test_storage_cleanup.py` (pytest, `unittest` is ruff-banned) carries the
  boot-path IMPORT guard.
  FIVE mutation tests run and confirmed fail-then-restore: epoch bug (5
  failures), shm age check (2), second `v_cruise_kph` writer (1), missing
  quantization (3), ungated re-derivation (2).
  TWO PRE-EXISTING TESTS WERE FOUND VACUOUS while updating them — both ran
  so few frames that the slew cap dominated and their two branches produced
  IDENTICAL values, so they would have passed with the feature deleted. Both
  now run to settling (n=200). Watch for this shape.
- ALSO NOTE: `sunnypilot/selfdrive/controls/lib/tests/test_lane_turn_desire.py`
  is DEVICE-ONLY (needs `params_pyx.so`). It appears to collect off-device
  only because a sibling test stubs `openpilot.common.params` into
  `sys.modules`; it then runs against a lying Params and reports meaningless
  failures. Exclude it alongside `test_cruise_mode.py`,
  `test_custom_cruise.py`, `test_speed_limit_resolver.py`,
  `test_auto_lane_change.py`.
- `FUNNYPILOT_VERSION` -> 3.4.5.
- ON-ROAD VERIFICATION REQUIRED / FALSIFIABLE: the ramp depends on OSM's
  `speedLimitAheadDistance` being a real metre value. If the next drive still
  shows the adjustment starting only AT the boundary, check the Verify code
  markers and `liveMapDataSP` freshness FIRST. Do not retune the ramp
  constants until the distance is confirmed sane — that is how v3.4.0 spent
  a whole release tuning a number that was never reaching the code.

### v3.4.4 Changes (based on funnypilot-3.4.3)

The longitudinal status dot answers the question it was BUILT to answer: are my
brake lights on? User report, verbatim: "it's showing red when we're commanding
any decceleration ... the dot goes red even if we're still using the throttle
but at a lower amount." Correct semantics: green = ANY throttle, gray = gas
gating / long inactive / coasting, red = ANY braking force (lamps lit).

KEY INSIGHT: `actuatorsOutput.accel` (SCC12 `aReqValue`) is a REQUEST to slow
down. On this platform the ESC alone decides whether to satisfy it by cutting
throttle or by pressing the brakes, so the sign of the command cannot answer
"are the lamps on". Any accel threshold that tries to is a coast model — i.e.
exactly the v3.3.9 mistake. The fix is to stop inferring and ASK THE CAR.

- `opendbc_repo/opendbc/sunnypilot/car/hyundai/carstate_ext.py` — classic-CAN
  `update()` now also reads `cp.vl["TCS13"]["BrakeLight"]`, the ESC's own
  brake-lamp output bit (high whenever the brakes are applied by ANY requester
  — driver pedal, ACC, AEB; low when merely off throttle). TCS13 is already
  parsed one line above for `aBasis` and the classic parser is built with an
  empty signal list (= all signals decoded), so this costs nothing on CAN.
  `BrakeLightPublisher` import is try/except-guarded: opendbc must stay
  importable without openpilot on the path (its own suite does that), and the
  publisher degrades to a no-op there.
- `sunnypilot/selfdrive/car/brake_light_shm.py` — NEW, stdlib-only.
  `/dev/shm/fp_brake`, one character `"0"`/`"1"`, atomic (mkstemp + os.replace).
  Same rationale as `sla_shm.py`: NO `cereal/*.capnp` change => no device
  rebuild (`CarState.brakeLights` exists upstream only as
  `brakeLightsDEPRECATED`). `BrakeLightPublisher.update()` writes on every
  transition (UI sees a brake within one frame) plus a `HEARTBEAT_FRAMES = 20`
  floor (5 Hz at the 100 Hz CarState rate). LOAD-BEARING INVARIANT:
  `read_brake_light()` returns **None = UNKNOWN**, never False, when the file
  is missing/garbage or older than `STALE_S = 1.0` — a dead publisher must not
  become a confident "brakes off".
- `selfdrive/ui/sunnypilot/onroad/long_status_dot.py` — `classify()` gained a
  4th arg `brake_light: bool | None`. Precedence: long inactive -> gray; lamp
  lit -> red (outranks a gate flag, so real braking during an approach is never
  masked); gas gating -> gray; lamp UNKNOWN + accel < -_EPS -> red (degraded
  fallback to the old rule, explicit rather than silent); accel > _EPS -> green;
  else gray. `_BRAKE_FIRM = -0.35` DELETED — it was the last coast-shaped
  threshold in the file, and a test now asserts it cannot come back. `_EPS`
  (0.02) is float/actuator noise around zero, not a physical model.
  KNOWN ASYMMETRY, deliberate and documented in the module docstring: the car
  publishes a brake lamp but no equally unambiguous "throttle applied" bit, so
  steady-state cruise holding speed with real throttle but ~zero accel command
  reads gray, not green. Fixing that honestly needs an engine-torque signal
  (`EMS16.TQI` / `TCS13.TQI_SCC`) whose "any throttle" boundary is not obvious
  — do NOT paper over it with another threshold.
- `opendbc_repo/opendbc/sunnypilot/car/hyundai/tests/test_brake_light_signal.py`
  — NEW (4 cases). BOOT-PATH GUARD, same class as the cruise_ext import test:
  `cp.vl[...]` raises KeyError on an unknown signal name, so a typo or an
  upstream DBC rename would not degrade the dot, it would kill card at 100 Hz
  = a car that does not drive. Parses `hyundai_kia_generic.dbc` as TEXT (no
  CANParser, no compiled extension) so it runs anywhere; includes an
  anti-vacuous self-check and asserts carstate_ext reads the exact name.
- `sunnypilot/selfdrive/car/tests/test_brake_light_shm.py` — NEW (13 cases):
  round-trip, garbage/empty -> None, stale -> None **even when the last value
  was True**, write-on-transition, heartbeat, rate limiting, and
  `HEARTBEAT_FRAMES/100.0 < STALE_S` (the freshness contract itself).
- `selfdrive/ui/sunnypilot/onroad/tests/test_long_status_dot.py` — rewritten
  (was 9 cases). `TestReducedThrottle` is the direct regression guard for this
  bug report; `TestUnknownLamp` pins None != False; `TestNoInferredPhysics`
  keeps the v3.3.9 signature guard and adds `_BRAKE_FIRM`/`get_coast_accel`
  absence.
- All three new guards MUTATION-TESTED (bug reintroduced -> suite fails ->
  restored). Import-light suite 200 green (was 179).
- `sunnypilot/navd/nav_webserver.py` — `EXPECTED_VERSION` -> "3.4.4"; two new
  `_CODE_MARKERS` rows (`class BrakeLightPublisher`, and `BrakeLight` in
  carstate_ext.py), 32 total. Markers are grepped inside single quotes, so they
  must stay free of quotes/metacharacters.
- `FUNNYPILOT_VERSION` -> 3.4.4.
- ON-ROAD VERIFICATION REQUIRED / FALSIFIABLE: this assumes the K5's ESC raises
  `TCS13.BrakeLight` for ACC-commanded braking, not only for the driver's
  pedal. If the next drive shows the dot staying gray through obvious
  openpilot braking, that assumption is dead — next suspects `SCC12.StopReq`
  and `TCS13.DriverOverride`. Do not add an accel threshold to compensate.

### v3.4.3 Changes (based on funnypilot-3.4.2)

Recovery + hardening. 3.4.2's annotation fix was correct but had never run on
the device; this branch verifies it on-road-ready, repairs the deploy path,
and generalizes the boot guard. Control behavior is UNCHANGED from 3.4.2 —
the SLA predictive set-speed ramp and the long status dot carry forward
byte-identical.

DEPLOY ROOT CAUSE (the reason flashes "didn't apply"): 1194 files inside
`/data/openpilot/.git` were owned by root, including `.git/HEAD`. Fetch over
HTTPS failed with `Permission denied` on `.git/logs/refs/...` and checkout
could not rewrite HEAD, so the `&&` deploy chain aborted before `reset --hard`
and the reboot — leaving old code running behind a command that looked
successful. NOT a credentials problem (the fetch remote is public HTTPS).
Fix: `sudo chown -R comma:comma /data/openpilot/.git`; never run `git` under
`sudo` on the device. Also: `git ... | tail` returns TAIL's exit code, so any
piped git check reports success unconditionally — use `${PIPESTATUS[0]}`.

- `sunnypilot/tests/test_capnp_annotations.py` — NEW, repo-wide AST guard
  (16 cases). Walks every .py file and flags capnp types (`car`/`custom`/
  `log`/`legacy`) used as a DIRECT operand of a `|` union, but ONLY in
  positions Python actually evaluates. THE RULE, verified empirically —
  parameter annotations RAISE, return annotations RAISE, **class-body
  annotated assignments RAISE** (this last form is a new finding; the
  previously recorded rule only mentioned parameters), while
  `self.CP: car.CarParams | None = None` in a method body and a plain local
  annotation are BOTH SAFE (not evaluated). DO NOT convert this to a grep:
  the safe forms are legitimately used in `selfdrive/ui/ui_state.py` and
  `selfdrive/ui/sunnypilot/ui_state.py`, and a text matcher would demand
  bogus "fixes" there. ALSO SAFE and deliberately not flagged: subscripted
  forms like `list[custom.X] | None` — `list[...]` builds a
  `types.GenericAlias` which DOES implement `__or__`, so the capnp object
  never gets `|` applied. `sunnypilot/models/fetcher.py:126` uses exactly
  that and is CORRECT; an early draft of the detector flagged it and the
  false positive was confirmed empirically before touching anything.
  Includes two self-checks so the scan can never pass vacuously.
- `sunnypilot/selfdrive/controls/lib/speed_limit/sla_shm.py`,
  `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py`,
  `sunnypilot/selfdrive/car/cruise_ext.py`,
  `selfdrive/ui/sunnypilot/onroad/long_status_dot.py` — comment/docstring
  CORRECTION only, no logic change. These claimed the `cereal/custom.capnp`
  change caused the v3.4.0 boot failure. It did not — the annotation did.
  The /dev/shm approach STAYS (no schema changes = no device rebuild is
  right on its own merits); only the stated justification changed.
- `sunnypilot/navd/nav_webserver.py` — `EXPECTED_VERSION` -> "3.4.3"; two new
  `_CODE_MARKERS` rows (`def write_sla_shm` in sla_shm.py, and
  `deliberately UNANNOTATED` in cruise_ext.py so the boot fix itself is
  verifiable from the Verify page).
- `.gitignore` — `.claude/` entry removed (Claude Code config now tracked).
- `CLAUDE.md` — all VPN/proxy remote-access documentation removed (no longer
  used); SSH Access is now just the home-network line, and Deploying to Device
  uses plain `ssh comma@192.168.86.31` plus a mandatory verification block.
- `FUNNYPILOT_VERSION` -> 3.4.3.

TESTING NOTE (off-device): use Python **3.11**, not the default 3.8 — the repo
uses PEP 585 generics and 3.8 fails collection with `'type' object is not
subscriptable`. Deps needed for the import-light suites: pytest, numpy,
pycapnp, setproctitle, zstandard, aiohttp, requests. Full command:
`cd /tmp && PYTHONPATH=<repo> <py311> -m pytest --noconftest -q -p no:cacheprovider -o addopts="" <paths>`
DEVICE-ONLY, always `--ignore` these (they need compiled extensions):
`test_cruise_mode.py`, `test_custom_cruise.py`, `test_speed_limit_resolver.py`,
`test_auto_lane_change.py`, `test_lane_turn_desire.py`.
v3.4.9: THERE IS NO LONGER A "KNOWN FAILURE" LIST. `long_v2/tests/test_physics.py`
was it, and it was deleted — it tested `k * sqrt(fric*g)` formulas that no
control code has used since v3.2.6e. Off-device with the deps above the
import-light suite is **348 green, 0 failed**. If something fails, it is real.
Both boot guards were MUTATION-TESTED (bug reintroduced -> suites fail ->
restored), per the "make the test fail with the bug present" rule.

### v3.4.2 Changes (based on funnypilot-3.4.1 — 3.4.0 AND 3.4.1 DID NOT BOOT)

THE REAL ROOT CAUSE of the unbootable car, after 3.4.1 guessed wrong:

    def update_speed_limit_assist_v_cruise_non_pcm(self, CS: car.CarState | None = None)
    TypeError: unsupported operand type(s) for |: '_StructModule' and 'NoneType'

RULE: **never put a capnp type in a `|` union.** `car.CarState`,
`custom.X`, `log.X` are capnp _StructModule objects, not Python types, and
`|` on them raises. Function PARAMETER annotations are evaluated when the
`def` executes (i.e. at import), so this kills the module at import time —
and cruise_ext sits on manager's startup path (manager -> process_config ->
mapd_manager -> osm_map_data -> base_map_data -> selfdrive.car.cruise ->
cruise_ext), so manager never started anything and the car sat on the comma
splash. Plain `x: car.CarState` (no union) is FINE. Attribute annotations
inside a method body (`self.CP: car.CarParams | None = None`, as in
ui_state.py) are also fine — Python does not evaluate annotations for
non-simple targets; verified by direct test, so do not "fix" those.

CORRECTION to the v3.4.1 section below: it blamed cereal/custom.capnp for
the failed boot. That was WRONG — the device log showed no build error,
only this TypeError, and 3.4.1 kept the annotation so it failed the same
way. The capnp revert + /dev/shm channel are kept (they work, avoid a
rebuild, and were explicitly requested) but they were NOT the fix.

- `sunnypilot/selfdrive/car/cruise_ext.py` — annotation removed; a comment
  at the signature explains why it must stay unannotated.
- `sunnypilot/selfdrive/car/tests/test_cruise_ext_imports.py` — NEW.
  PROCESS LESSON: no test imported cruise_ext, so two consecutive releases
  went out "163 green" and bricked the car both times. A module on the boot
  path needs an import test, not just logic tests. Two guards here: a real
  import (compiled-only deps stubbed) and a source scan for capnp-in-union.
  Both verified to FAIL with the bad annotation reintroduced.
- `FUNNYPILOT_VERSION` -> 3.4.2, EXPECTED_VERSION -> "3.4.2". Suite 163.

### v3.4.1 Changes (based on funnypilot-3.4.0 — capnp revert; did NOT fix the boot, see 3.4.2)

HARD-WON RULE, read before adding any cross-process field: **do not change
`cereal/*.capnp` on this fork unless you intend a device rebuild.** v3.4.0
added 4 fields to custom.capnp; that forces SCons to regenerate + recompile
the schema at next boot, and the car hung on the comma splash screen,
unrecoverable by restarting (SSH still worked — that is how it was fixed).
Everything these features touch is Python (plannerd, card) or Python+raylib
(UI), so there was never a reason to pay a compile.

- `cereal/custom.capnp` — REVERTED to byte-identical with 3.3.8. Verified:
  `git diff funnypilot-3.3.8 -- cereal/` is empty, and the full diff vs
  3.3.8 contains only .py and .md files. Existing prebuilt binaries stay
  valid; no rebuild is triggered.
- `sunnypilot/.../speed_limit/sla_shm.py` — NEW. `/dev/shm/fp_sla`, one line
  `"<v_cruise_target_mps>,<gas_gate 0|1>"`, written by plannerd at 20 Hz.
  Carries the only two values that must cross a process boundary: the SLA
  set-speed ramp target (plannerd -> card/cruise_ext) and SLA's gas-gate
  flag (plannerd -> UI dot). Same pattern as controlsd's /dev/shm/lat_interp.
  Writes are atomic (tempfile + os.replace) so readers never see a torn
  line; reads are best-effort and return (0.0, False) = "no request" on
  missing/garbage input, so this channel can never break control.
- `sunnypilot/.../longitudinal_planner.py` (SP) — publishes via
  `write_sla_shm(...)` instead of the removed capnp fields.
- `sunnypilot/selfdrive/car/cruise_ext.py` — reads the ramp target with
  `read_sla_shm()` inside `update_speed_limit_assist` (LP_SP rate, NOT per
  100 Hz control frame).
- `selfdrive/ui/sunnypilot/onroad/long_status_dot.py` — SCC-V/SCC-M gate
  flags still come from capnp (they already existed); SLA's comes from the
  shm file.
- All v3.4.0 BEHAVIOR is unchanged — see the v3.4.0 section below for the
  two post-mortems that still apply (wrong-layer SLA ramp, pitch-derived
  status dot). `FUNNYPILOT_VERSION` -> 3.4.1, EXPECTED_VERSION -> "3.4.1".
  Suite 161 green.

### v3.4.0 Changes (based on funnypilot-3.3.8; v3.3.9 ABANDONED — and 3.4.0 itself did not boot, see 3.4.1)

v3.3.9 IS DEAD — do not flash it, do not carry its code forward. Both of
its features were built on wrong premises and were re-done from scratch
here, branching from 3.3.8. Its two post-mortems are the most valuable
part of this section; read them before touching either feature.

POST-MORTEM 1 — SLA under DEC. v3.3.9 added `long_v2/sla_ramp.py`
(`SlaSpeedRamp`), which pre-ramped the `v_cruise` value handed to the MPC.
That was the WRONG LAYER: in DEC's blended mode `v_cruise` enters only as a
position cap weighted 0.1 against the model's own accel plan at 5.0, so
shaping it changed almost nothing — the exact root cause it was written to
fix. The correct layer is the ACTUAL CRUISE SET SPEED, which every mode
honors identically (acc: cruise obstacle; blended: position cap; plus it is
what the cluster shows). LESSON: when a value is weakly weighted inside the
solver, shaping it more smoothly does not make it matter more — move the
quantity that the whole stack already agrees on.

POST-MORTEM 2 — the status dot. v3.3.9 read the commanded accel but then
compared it against `get_coast_accel(pitch)`, an IMU-derived road-grade
estimate, to decide "braking". That made a command-layer readout into a
sensing-layer one — precisely what it was supposed to avoid. LESSON: if the
requirement is "show me what we told the car", the decision must contain no
estimated/derived physical quantity at all.

- `sunnypilot/.../speed_limit/speed_limit_assist.py` — NEW
  `_update_cruise_ramp()` (v3.4.0 marker = `_update_cruise_ramp` grep
  target). Publishes `v_cruise_target`: the set speed the CLUSTER should
  read right now. Down into a slower zone, constant-decel envelope
  `sqrt(next^2 + 2*RAMP_DECEL*d_eff)` (RAMP_DECEL 0.8, distance-based so
  there is no v_ego division / standstill blowup), converging on the new
  target at the boundary. Up into a faster zone, linear over the last
  RAMP_UP_DIST (90 m) — this DOES raise the set speed slightly before the
  sign, which is the explicit user request ("increase the max speed when
  approaching a higher speed limit"); the window is deliberately short.
  Slew-capped by RAMP_MAX_RATE (4.0) so the displayed number never jumps.
  LOAD-BEARING: `_cluster_change_is_ours()` + `_commanded_conv` /
  `_prev_commanded_conv`. SLA re-derives the driver's carried offset ratio
  from ANY cluster change (`update_state_machine`); mid-approach the
  cluster sits BETWEEN zones, so re-deriving there would silently collapse
  a carried +20% to whatever the ramp is passing through. The guard
  suppresses re-derivation ONLY for values the ramp itself commanded (one
  frame of history covers the plannerd->card->carState round trip). Genuine
  button presses land elsewhere and re-derive normally; the boundary snap
  is still idempotent. Unit-tested directly (TestRatioPreservedDuringRamp).
  SLA's zone/ratio state machine is otherwise UNTOUCHED.
- `sunnypilot/selfdrive/car/cruise_ext.py` —
  `update_speed_limit_assist_v_cruise_non_pcm(CS)` (signature gained CS)
  now, in addition to the existing boundary snap, follows
  `assist.vCruiseTarget` every frame while SLA is active, writing
  `v_cruise_kph` in whole display units — i.e. mimicking cruise button
  taps. `_ramp_hold_frames` pauses the ramp ~1 s after ANY cruise button
  event so the driver's own adjustment survives long enough for SLA to see
  it and re-derive the ratio (without this the ramp would overwrite the
  press on the very next 100 Hz frame and the press would be lost).
- `selfdrive/car/cruise.py` — call site passes `CS`.
- `sunnypilot/.../longitudinal_planner.py` (SP) — publishes the four new
  capnp fields.
- `cereal/custom.capnp` — `SpeedLimit.Resolver` += `nextSpeedLimitFinal @9`,
  `distToNextSpeedLimit @10` (upcoming-zone info must reach cruise_ext, in
  the card process, to drive the ramp); `SpeedLimit.Assist` +=
  `gasGating @7` (SLA's pre-zone gate, previously internal-only — the
  status dot needs it), `vCruiseTarget @8`.
- `selfdrive/ui/sunnypilot/onroad/long_status_dot.py` — NEW, always-on
  (NOT dev-UI-gated) bottom-left dot. Reads
  `carOutput.actuatorsOutput.accel` — for this car the literal `aReqValue`
  packed into SCC12 (hyundai carcontroller: `new_actuators.accel =
  self.tuning.actual_accel`), i.e. the last software layer before the car.
  gray = neither commanded / long inactive / gas gating, red = any
  commanded decel, green = any commanded accel. Gas gating -> gray comes
  from the controllers' OWN published booleans (SLA `assist.gasGating`,
  SCC-V/SCC-M `gasGating`), never from recognizing a coast-shaped value;
  `_BRAKE_FIRM` (-0.35) makes real braking win over a gate flag so a brake
  application during an approach is never masked gray. Decision logic is
  the pure `classify(long_active, accel, gas_gating)`.
- `selfdrive/ui/sunnypilot/onroad/tests/test_long_status_dot.py` — NEW (9
  cases), loads `classify()` with pyray/ui_state stubbed (onroad modules
  can't import off-device). Includes a signature regression guard that
  classify takes NO pitch/measured-accel argument — the v3.3.9 mistake.
- `sunnypilot/.../speed_limit/tests/test_sla_cruise_ramp.py` — NEW (11
  cases), reuses test_speed_limit_assist.py's import-light harness:
  envelope walks down / never exceeds the current zone, up-ramp engages
  only inside the window and never overshoots, slew bound under an
  adversarial late-appearing zone, and the three ratio-preservation cases.
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> "3.4.0"; new
  markers (`_update_cruise_ramp`, `class LongStatusDotRenderer`).
- `FUNNYPILOT_VERSION` -> 3.4.0. Full import-light suite 161 green.

### v3.3.8 Changes (based on funnypilot-3.3.6; 3.3.7 skipped per user request)

Lateral: the turn-in "grab torque / fail to hold it / re-bite" oscillation
addressed at its mechanism. Longitudinal: E2E experimental restored as a
real mode, DEC-compatible, with every fork governor working in both modes.

- `selfdrive/controls/lib/eps_limit.py` — NEW, stdlib-only.
  `EpsTorqueGovernor`: mirrors the K5's carcontroller/panda driver-torque
  clamp (allowed = 384 + (50 − |CS.steeringTorque|·1)·2, K5 classic-CAN
  constants duplicated from opendbc hyundai values.py — update together)
  and the +3/−7-per-10 ms slew, applied LAST in latcontrol_torque so the
  request that leaves the controller is ALWAYS realizable by the rack
  (unit-tested as identity against the real opendbc
  apply_driver_steer_torque_limits). Bounds collapse instantly (hardware
  clamps this frame regardless) but recover at RECOVERY_RATE = 0.35/s —
  under half the hardware's 0.78/s — the damping that breaks the
  bite → sensor-spike → shed → re-bite limit cycle. HYPOTHESIS STATUS
  (user-corrected 2026-07-19): the "wheel-inertia trips the sensor"
  story is UNVERIFIED — the v3.2.8-era 150+ figure was inferred from
  steeringPressed latching, never read from logs, and the 3.2.8 fix
  built on it did NOT cure the oscillation. Do not cite it as measured.
  Code facts that hold regardless: the clamp exists, threshold sensor
  50, and it engages from 50–150 where NOTHING else fires
  (steeringPressed threshold 150, slb only when panda blocks) — before
  v3.3.8 the integrator wound against an invisible limit whenever it
  did engage.
  `driver_limited` (prev frame) now freezes the PID integrator and ORs
  into the saturation check (_check_saturation dwell keeps transients
  out of the alert). Governor can only reduce torque; panda backstop
  untouched. reset() on inactive => re-engage ramps from 0 at the
  hardware rate, matching what the carcontroller does anyway.
- `selfdrive/controls/lib/latcontrol_torque.py` — governor instantiated
  + applied in the ACTUATOR frame (`-self._eps_governor.update(
  -output_torque, CS.steeringTorque)` — the sign flip is LOAD-BEARING,
  CS.steeringTorque and actuators.torque share the actuator frame while
  latcontrol's internal torque is negated). freeze_integrator gains
  `or self._eps_governor.driver_limited`.
- `selfdrive/controls/lib/triage_recorder.py` — lat records gain three
  hypothesis discriminators: "eps" (per-second MIN governor authority;
  1.0 = clamp never engaged), "dtx" (per-second MAX |raw
  CS.steeringTorque| — the direct measurement the 3.2.8 era never
  took), "tqd" (per-second MAX |requested − applied| torque via
  carOutput, one-frame lag). TRIAGE MATRIX for a drive with
  oscillation events, hands off: (A) dtx > 50 + eps < 1.0 pulses at
  the events => driver-torque clamp loop CONFIRMED, tune governor
  recovery from data. (B) dtx < 50, eps pinned 1.0, tqd ≈ 0, but
  oscillation felt => clamp falsified; suspect EPS-internal derate
  (rack fades sustained torque invisibly — next step ALT_LIMITS-style
  sustained cap, deliberately NOT added in 3.3.8) or our own request
  oscillating (check tqx swing + lat_interp hmin). (C) tqd large
  without eps dips => panda-side stripping we didn't model. controlsd
  passes all via getattr chain (angle/PID tuning cars => eps 1.0).
- `selfdrive/controls/lib/tests/test_eps_limit.py` — NEW (10 cases):
  realizability-vs-real-opendbc-clamp over randomized sequences,
  hardware-exact bound at sensor 150 (= 184/384), recovery strictly
  slower than hardware, aligned-torque no-op, symmetry, reset, NaN,
  never-amplifies. A closed-loop plant sim was inconclusive (toy plant
  can't reproduce the measured sensor regime honestly) — on-road "eps"
  triage is the verification path.
- `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` — `mode`
  ('acc'/'blended') RESTORED (v3.3.8 marker; the v3.2.6e rewrite had
  deleted it, reducing E2E to min(action.desiredAcceleration, ACC-MPC)
  — which cannot accelerate when the bundle's action accel is
  empty/meaningless => the "pure E2E won't accelerate whatsoever"
  report). 'acc' path byte-identical to 3.3.6 (fork T_FOLLOW etc.);
  'blended' = upstream verbatim: cost set [0, .1, .2, 5, 40, 1] (no
  personality jerk factor — the model's plan owns the shape), MPC
  tracks model x/v/a/j yref, cruise POSITION cap
  (T_IDXS·clip(v_cruise, v_ego−2, ∞) + x[0]), danger factor 1.0,
  post-solve lead0/lead1 source detection (lead1 requires nearer than
  lead0 — deliberate tightening of upstream's truthiness check).
  update() signature gained x, v, a, j. Runtime-only params/weights —
  prebuilt aarch64 solver untouched, but blended behavior itself needs
  on-device validation (acados absent in CI).
- `selfdrive/controls/lib/longitudinal_planner.py` — upstream mode
  selection restored, DEC-arbitrated: mode = experimental ? 'blended' :
  'acc'; DEC (when active) overrides via get_mpc_mode(); mpc.mode only
  set for non-mlsim bundles (mlsim keeps ACC-MPC + min-blend, exactly
  upstream). parse_model x/v/a/j now fed to mpc.update. Output blend:
  `mode == 'acc' or not mlsim` => pure MPC, else min(e2e, mpc).
  DEC-COMPATIBILITY BY CONSTRUCTION: SCC-V/M, SLA and
  HIDDEN_CRUISE_OFFSET shape v_cruise BEFORE the MPC (cruise obstacle
  in acc / position cap in blended), and the SLA gas gate, shaper,
  LeadGrace and fork accel_clip act on the output path in every mode.
  DELIBERATE fork divergences from upstream kept: the 70% accel clip +
  turn limiting apply in blended too (upstream unclips to ACCEL_MAX);
  gentle is the fork's identity.
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` —
  `is_e2e` replaced by upstream's `mlsim` property (generation None or
  >= 11) + `get_mpc_mode` (None unless dec.active()). NOTE: DEC's
  slowness detection compares v_ego to DISPLAYED vCruise, so the hidden
  0.93 governor makes _has_slowness ~always true while cruising —
  harmless (slowness is checked after slow_down/standstill and both
  request 'acc' anyway); left untouched on purpose.
- `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` — badges
  scaled by _BADGE_SCALE = 1.08 (font/paddings/min_width/sub-label).
  Arbitration display: reads longitudinalPlanSP.longitudinalPlanSource
  (custom capnp enum) — when both SCC-V and SCC-M are constraining
  (active + vTarget < 888), the source-winner's color lerps toward
  _COLOR_WIN (blue) + white ring (draw_rectangle_rounded_lines_ex, same
  API as sidebar.py), the loser toward _COLOR_LOSE (slate), tint
  strength = |vTarget split| / _DISAGREE_FULL_MS (3 m/s = full). Base
  state colors (disabled/armed/gas-gate/braking) unchanged and shown
  whenever they agree. _BadgeState's 6-frame lerp naturally smooths the
  continuously-moving gradient targets.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` —
  LatInterpolElement DELETED (static "5"; spline is knot-exact by
  construction). NEW EpsLimitElement ("EPS", % authority, 1 s min-hold
  via _RollingExtreme; green >= 0.99 / orange >= 0.60 / red below,
  "-" when lat inactive) reading the v3.3.8 "n,authority" format of
  /dev/shm/lat_interp; NEW DriverTorqueElement ("TBAR", 1 s max-hold
  of |carState.steeringTorque|; green < 50 / orange 50-149 (the clamp
  band steeringPressed can't see) / red >= 150). GLANCE RULE for the
  user: all-green = normal; during an oscillation event TBAR orange+
  with EPS < 100 confirms the clamp mechanism, TBAR green with EPS 100
  falsifies it (matches the triage dtx/eps matrix).
- `selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py` — bottom
  bar leftmost (torque cars): EPS + TBAR replace INTERP.
- `selfdrive/controls/controlsd.py` — /dev/shm/lat_interp heartbeat now
  writes "n,authority" at 20 Hz, authority = MIN governor bound since
  the last model frame (reset after each write; getattr chain so
  non-torque tuning writes 1.0).
- `sunnypilot/navd/nav_webserver.py` — EXPECTED_VERSION -> "3.3.8";
  new markers ("class EpsTorqueGovernor" in eps_limit.py, "v3.3.8" in
  long_mpc.py, "class EpsLimitElement" in dev-UI elements.py);
  eps_limit.py added to _FEEL_FILES hashes.
- `FUNNYPILOT_VERSION` -> 3.3.8. Full import-light suite 127 green.

#### v3.3.8 continued (2026-07-21): SCC-M/V arbitration + bump hypothesis

USER CORRECTION, binding: the EPS torque governor above is NOT confirmed
as the (sole) cause of the turn-in oscillation — user reports it still
occurs crossing railroad tracks mid-corner with the dev-UI EPS readout
pinned at 100%, i.e. the driver-torque clamp was NOT engaged during those
events. The clamp fix stands (correct whenever the clamp does engage) but
does not explain this case. User's alternate hypothesis — NOT YET
VERIFIED, do not act on it beyond instrumentation until it correlates on
a drive: crossing the tracks unloads the front/steering axle (weight
transfer), which could reduce grip or reduce the self-aligning torque
needed for a given angle (so a previously-correct torque overshoots the
angle), and suspension rebound continues disturbing the front axle for a
beat afterward — a physics explanation for a REPEATING event rather than
a single snap. Per this project's repeated history (v3.2.8, v3.2.9e,
v3.2.12, v3.3.2 — every one shipped a fix on inferred causes and had to
be revised or reverted after real driving data), the discipline here is
instrument -> drive -> correlate -> THEN fix, not guess again.

- `sunnypilot/selfdrive/controls/lib/long_v2/speed_governor.py` — NEW
  `gate_map_target(map_v_target, vision_is_active)`: returns CAP_INACTIVE
  unless SCC-Vision is also actively constraining. SCC-M can now only
  narrow SCC-V's cap, never introduce a slowdown vision doesn't share
  (map data — mistagged/rounded curve speeds, stale OSM — is far more
  false-positive-prone than the model's own view of the road). Vision
  keeps full independent authority. Pure function, no cereal deps, so
  it's testable without the device's compiled msgq/capnp extensions.
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` (SP) —
  `update_targets` calls `gate_map_target(self._scc_map_v2.output_v_target,
  self._scc_vision_v2.is_active)` before handing v_scc_map to the
  governor. `self._scc_map_v2.output_v_target` itself is UNCHANGED (still
  the raw computed cap) — only what reaches the governor is gated, so
  debug/UI can still see what map "wanted."
- `sunnypilot/selfdrive/controls/lib/long_v2/tests/test_scc_gating.py` —
  NEW (3 cases): map-alone ignored, map-confirmed-by-vision passes
  through unchanged, both-inactive stays inactive.
- `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` — badges
  scaled by _BADGE_SCALE = 1.08 (font/paddings/min_width/sub-label).
  Arbitration display: reads longitudinalPlanSP.longitudinalPlanSource
  (custom capnp enum) — when both SCC-V and SCC-M are constraining
  (active + vTarget < 888), the source-winner's color lerps toward
  _COLOR_WIN (blue) + white ring (draw_rectangle_rounded_lines_ex, same
  API as sidebar.py), the loser toward _COLOR_LOSE (slate), tint
  strength = |vTarget split| / _DISAGREE_FULL_MS (3 m/s = full). Base
  state colors (disabled/armed/gas-gate/braking) unchanged and shown
  whenever they agree. _BadgeState's 6-frame lerp naturally smooths the
  continuously-moving gradient targets.
- `selfdrive/controls/controlsd.py` — /dev/shm/lat_interp heartbeat
  extended to "n,authority,pitch": pitch = MAX car-frame Y-axis angular
  rate magnitude (deg/s, ~pitch rate) since the last model frame, read
  from `self.calibrated_pose.angular_velocity` — ALREADY computed every
  frame for carControl's orientationNED/angularVelocity, so this is zero
  new subscription cost. Triage `sample()` gains `pitch_rate_deg`.
- `selfdrive/controls/lib/triage_recorder.py` — lat records gain "pit"
  (per-second peak |pitch rate|, deg/s). Explicitly a diagnostic-only
  field — no threshold/alert logic attached.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — NEW
  `SuspensionBumpElement` ("BUMP", °/s, 1 s max-hold via the same
  `_RollingExtreme` helper EPS/TBAR use). DELIBERATELY uncolored (plain
  white) — no claimed-confident thresholds exist yet for this signal;
  the point is to read the raw number, not trust a color. `_read_lat_interp`
  now parses the 3-field heartbeat and returns `(n, authority, pitch)`.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py` — bottom bar
  (torque cars) gains BUMP after EPS/TBAR.
- `sunnypilot/navd/nav_webserver.py` — new markers (`gate_map_target` in
  speed_governor.py, `class SuspensionBumpElement` in elements.py).
- NEXT STEP for the user: next drive, watch BUMP (and later, correlate
  triage "pit") against felt oscillation events, alongside EPS/TBAR. A
  pit spike coinciding with an event AND eps staying at 100% supports
  the weight-transfer theory over the torque-clamp theory for that
  event; no pit spike falsifies it too, and we look elsewhere (tire/
  alignment, a genuinely separate mechanism, or the model's own plan
  reacting to the visual disturbance of the tracks).
- Full import-light suite 130 green (127 + 3 new gating tests).

#### v3.3.8 continued (2026-07-21): bump damper — an ACTED-ON fix + LIM readout

Real on-road data arrived: BUMP peaked to 7°/s exactly at a railroad-track
crossing (baseline <3°/s), EPS went orange (clamp DID engage that time,
unlike the prior crossing where it stayed pinned at 100%), and the felt
oscillation started AFTER BUMP had decayed back to 2-3°/s — a LAG, not
simultaneity. USER EXPLICIT DIRECTION: stop instrumenting, consult a
second model (Fable-5, full repo access) with the complete history, and
ship a fix. This section is that fix — ACTED-ON, not fully proven; the
mechanism below was independently verified against the real code (exact
constants, line references) before implementing, not just asserted.

- `selfdrive/controls/lib/bump_damper.py` — NEW, stdlib-only.
  `BumpDamper`. MECHANISM (three code-verified facts, not guesses):
  (1) `opendbc/car/lateral.py:get_friction()`'s slope inside its
  threshold band = `friction * latAccelFactor / FRICTION_THRESHOLD`;
  for the K5 (fitted friction 0.1165, threshold 0.2) with the fork's
  LOCKED latAccelFactor 2.750 that's ~1.6 — TWICE the PID's own KP of
  0.8 — live exactly in the small-error turn-in regime. (2) That same
  locked 2.750 is ~14% above the K5's FITTED 2.405
  (`opendbc/car/torque_data/params.toml`, legend confirms column
  order); since torque = lat_accel / latAccelFactor, locking it higher
  makes feedforward under-deliver ~12.5% of the torque actually
  needed, pushing more work onto the high-gain relay in (1). (3) The
  PID setpoint is the model's desire from ~lat_delay (~0.5s) ago (the
  delay buffer), and the jerk lookahead replays the same buffer
  ~0.3s later — so a bump disturbance corrupts the MEASUREMENT during
  the event and the delay-buffered SETPOINT 0.3-0.5s afterward,
  landing in the high-gain relay window long enough to ring — this is
  the mechanistic explanation for the observed peak-then-lag pattern.
  On a pitch-rate spike (TRIGGER_DEG_S=5.0, between the confirmed
  <3 baseline and the 7 event peak), blends `measurement` TOWARD
  `setpoint` (shrinks |error| — does NOT hold measurement, which under
  a ramping setpoint would manufacture GROWING error = MORE torque,
  backwards) for HOLD_S=0.8s past the last supra-threshold frame (covers
  the ~0.5s delayed-setpoint arrival) plus RECOVER_S=0.7s linear
  recovery, floor MIN_DAMP=0.40 (matches the fork's other floors:
  lane-change 0.45, override 0.6). Only ever softens toward the plan's
  own feedforward — cannot add torque, cannot lose the corner.
- `selfdrive/controls/lib/latcontrol_torque.py` — `self._bump_damper`
  instantiated + reset on inactive (matches eps_governor/override_gate
  convention). Blend applied right after `error = setpoint -
  measurement` is first computed, BEFORE the friction/ff line, so both
  the correction AND the friction relay's argument are damped; the
  jerk-lookahead term feeding that same relay is separately scaled by
  `damp`. `freeze_integrator` gains `or self._bump_damper.active`.
  `raw_measurement` preserved for `pid_log.actualLateralAccel` so logs
  stay honest (not damped). Propagates automatically into the NNLC
  extension path (`LatControlTorqueExt.update()` receives the same
  local `measurement` variable by reference) when NNLC is enabled —
  verified by reading `nnlc.py`; NNLC is a pure passthrough when
  disabled (the K5's actual default config), so for this car the base
  LatControlTorque path IS the whole fix.
- `sunnypilot/selfdrive/controls/lib/nnlc/nnlc.py` — defensive addition:
  `update_output_torque`'s OWN separate `freeze_integrator` (a
  pre-existing gap — it already missed the EPS governor's flag too)
  now also ORs in both `_eps_governor.driver_limited` and
  `_bump_damper.active` via `self.lac_torque`, for consistency if NNLC
  is ever toggled on.
- `selfdrive/controls/lib/tests/test_bump_damper.py` — NEW (11 cases):
  no-op below trigger, instant collapse (both pitch signs), hold
  duration, linear recovery, full recovery to exact no-op, retrigger
  extends hold, bounds, NaN/inf safety, reset. A closed-loop plant sim
  (delayed-PID + friction-relay under a step measurement disturbance,
  not committed to the repo) showed ~74% less post-bump torque ringing
  and ~60% lower peak error with the damper — a logic sanity check,
  not proof the real vehicle behaves this way.
- FALSIFIABLE with the EXISTING BUMP readout, zero new instrumentation:
  next drive, if the oscillation still occurs while BUMP shows
  >5°/s (damper provably engaged that frame), this mechanism is dead —
  next suspect is the model's own plan, not the controller's reaction.
- `selfdrive/controls/controlsd.py` — /dev/shm/lat_interp heartbeat
  extended again: "n,authority,pitch,limited" — limited = fraction of
  control frames since the last model frame where
  `EpsTorqueGovernor.driver_limited` was true (distinct from
  `authority`, the ceiling — a bound can sit below 100% without ever
  actually being hit).
- `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py` — L.S.
  wiring removed (class kept, unused); NEW `TorqueLimitActiveElement`
  ("LIM", %, 1s max-hold): 0% green = request passing through
  unmodified, <50% orange, >=50% red. `_read_lat_interp` parses the
  4-field heartbeat.
- `selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py` — LIM
  inserted right after EPS in the torque diagnostic group (EPS, LIM,
  TBAR, BUMP); L.S. no longer drawn.
- `sunnypilot/navd/nav_webserver.py` — new marker (`class BumpDamper`
  in bump_damper.py); bump_damper.py added to `_FEEL_FILES` hashes.
- Full import-light suite 141 green (130 + 11 new bump-damper tests).

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
