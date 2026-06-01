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
