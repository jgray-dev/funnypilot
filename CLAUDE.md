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

## Branching Policy (v0.9.5+)

All versions must be separate branches: `funnypilot-0.9.X`
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

### v1.0.5 Changes

- `sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py` - Fixed dynamic speed limit assist resetting to 0% on speed limit changes. Prevented the offset ratio from recalculating when the system automatically sets the `v_cruise_cluster` value, requiring manual user interaction to update the ratio.
- `sunnypilot/selfdrive/controls/lib/longitudinal_planner.py` - Added call to `self.sla.update_car_state(CS)` so button states and cruise changes can be accurately monitored.
- `selfdrive/ui/onroad/model_renderer.py` - Safely access `navInstruction.lanes` using `getattr` to prevent UI crash on unpopulated fields.
- `system/updated/updated.py`, `selfdrive/ui/sunnypilot/layouts/settings/software.py` - Updated `_is_funnypilot_branch` to also allow fetching and selecting upstream `main`, `dev`, and `staging` branches. Fixed regex to support branch suffixes properly.

### v1.0.4ar Changes

- `selfdrive/ui/sunnypilot/onroad/navigation_panel.py` - Replaced the boxed nav card with a minimal AR-style overlay: projected lane ribbon, target lane highlight, horizon turn cue, compact textual guidance, and lane-change status.
- `selfdrive/ui/onroad/model_renderer.py` - Added camera-calibrated AR navigation markers projected in road space (turn chevrons + guide line + compact distance/ETA label) using the existing model path transform.
- `sunnypilot/navd/navigationd.py` - Increased nav daemon loop rate from 3 Hz to 5 Hz for snappier on-device updates.
- `cereal/services.py` - Raised `navInstruction` and `navigationStateSP` service metadata from 1 Hz to 5 Hz to match higher-rate publishing.
- `FUNNYPILOT_VERSION`, `CHANGELOG.md` - Bumped branch version marker to `1.0.4ar` and documented the AR navigation + responsiveness update, including UI-side dead-reckoned distance smoothing.

### v1.0.2m Changes

- `sunnypilot/navd/mapbox_config.py` - New token loader for Mapbox credentials; reads from env (`MAPBOX_PUBLIC_TOKEN`, `MAPBOX_SECRET_TOKEN`) or secret files (`.nav_secrets/mapbox_tokens.json` and device paths) without committing keys.
- `sunnypilot/navd/routing/osrm_client.py` - Routing now prefers Mapbox Directions `driving-traffic` (traffic-aware) and falls back to public OSRM on failure, preserving lane/secondary maneuver extraction.
- `sunnypilot/navd/routing/geocoder.py` - Autocomplete/reverse geocoding now use Mapbox Geocoding first, with Photon/Nominatim fallback.
- `sunnypilot/navd/nav_webserver.py` - Added `/api/config` for web token bootstrap and `/api/route_preview` for backend route geometry fetches.
- `sunnypilot/navd/nav_web/index.html`, `sunnypilot/navd/nav_web/app.js`, `sunnypilot/navd/nav_web/style.css` - Migrated nav web UI from Leaflet to Mapbox GL JS dark globe visuals with route overlay rendering.
- `selfdrive/ui/sunnypilot/onroad/navigation_panel.py` - Expanded lane guidance with a top-down lane map, target-lane highlighting, and lane-intent status labels driven by nav lanes + model lane-change desire/blinker state.
- `scripts/setup_mapbox_tokens.py`, `.gitignore` - Added helper to install local/device token files and excluded `.nav_secrets/` from git tracking.

### v1.0.2 Changes

- `sunnypilot/navd/routing/osrm_client.py` - Route parsing now extracts per-step lane guidance from OSRM intersections (`lanes`, `activeDirection`) and stores richer secondary maneuver context (`destinations`, `exits`, `ref`).
- `sunnypilot/navd/nav_state.py` - `current_instruction()` now publishes lane data, `maneuverSecondaryText`, `showFull`, and a compact upcoming maneuver queue (`allManeuvers`) with cumulative distances.
- `sunnypilot/navd/navigationd.py` - Nav publisher now fills advanced `navInstruction` fields (`maneuverSecondaryText`, `showFull`, `lanes`, `allManeuvers`) with enum-safe direction mapping and defensive guards to avoid navd crashes from malformed payloads.
- `selfdrive/ui/sunnypilot/onroad/navigation_panel.py` - Added desired-lane HUD rendering (active lane highlight + compact lane direction boxes), dynamic panel sizing when lane guidance is present, and safe lane decoding from `navInstruction`.
- `sunnypilot/selfdrive/controls/lib/nav_lane_change_assist.py` - New nav-aware auto lane-change helper that emits virtual blinker requests for highway maneuvers (`off ramp`, `on ramp`, `fork`, `merge`) with speed, distance-window, cooldown, and one-trigger-per-maneuver gating.
- `selfdrive/controls/lib/desire_helper.py`, `selfdrive/modeld/modeld.py` - Integrated nav-aware lane-change assist into desire generation by feeding `navInstruction` into `DesireHelper.update()`, while preserving manual blinker priority and existing lane-turn behavior.

### v1.0.1 Changes

- `selfdrive/ui/sunnypilot/onroad/nav_quick_access.py` - Fixed Widget visibility bypass: changed `self.visible = True/False` to `self.set_visible(not in_drive)` so Widget's `_is_visible` is properly managed. Removed manual `if not self.visible: return` from `_render()`. Added `_PARAMS_REFRESH_INTERVAL = 5.0s` throttle for NavHomeLocation/NavWorkLocation param reads. Moved `import math` to module level.
- `selfdrive/ui/sunnypilot/onroad/navigation_panel.py` - Moved `import math` to module level (was inside `_draw_angled_arrow` function body). Wrapped `sm.updated` access in try/except in `update()`.
- `selfdrive/ui/sunnypilot/onroad/hud_renderer.py` - Added try/except around nav widget `update()` and `render()` calls so any nav widget exception cannot propagate and crash the UI process.
- `sunnypilot/navd/nav_webserver.py` - Added `reuse_address=True` to `web.run_app()` to prevent port 8888 TCP TIME_WAIT bind failures on rapid restart.

### v1.0.0 Changes

- `sunnypilot/navd/nav_web/` - Completely redesigned UI: true dark mode (`#000`), replaced system emojis with inline SVGs, integrated sleek map tile inversion filtering, and modernized layout components (status badge, search box, dest panel).
- `sunnypilot/navd/navigationd.py` - Fixed route bootstrap race: if destination is set before GPS becomes valid, daemon now automatically fetches the route once GPS lock arrives. Refactored destination parsing into `_load_destination()` and reused for reroute path.
- `sunnypilot/navd/nav_state.py` - Resets `distance_to_maneuver` on `set_route()` so reroutes/new routes do not briefly show stale maneuver distance.
- `sunnypilot/navd/nav_web/app.js` - Hardened coordinate validation with `hasNumber()` checks. Correctly supports valid `0` lat/lon values (equator/prime meridian) for GPS, destination markers, Home/Work validation, and map recentering.
- `sunnypilot/navd/routing/route_cache.py` - New offline route cache + recovery helpers. Stores fetched routes on disk as gzip with index pruning (max route count + total size cap), loads best cached route when online fetch fails, tracks breadcrumbs, and builds temporary backtrack/rejoin routes while offline.
- `sunnypilot/navd/navigationd.py` - Integrated cache-aware route fetch (`online -> cache fallback`) and breadcrumb-driven offline rejoin route generation on off-route events when reroute API is unavailable.
- `system/updated/updated.py` - Updater now supports FunnyPilot branch workflows: detects/prefers `funnypilot` remote for version branches (`funnypilot-X.Y.Z[h]`), fetches/checkouts from the correct remote, and publishes staged progress in `UpdaterState` for UI progress bars.
- `selfdrive/ui/sunnypilot/layouts/settings/software.py`, `selfdrive/ui/layouts/settings/software.py` - Software panel now includes a FunnyPilot branch refresh button, version-only branch picker, automatic fetch on selection, and download/finalize progress bar parsing from updater state tokens.
- `selfdrive/ui/layouts/sidebar.py` - Improved local IP detection for nav web display with fallback strategy that works better in offline/no-internet scenarios.
- `sunnypilot/navd/nav_web/app.js` - Added Leaflet availability guard so web UI stays functional for non-map controls if CDN map assets fail offline.
- `PUSH100.sh` - Local-LAN deployment script for branch `funnypilot-1.0.0` (`ssh comma@192.168.86.31`) to push, checkout/reset, verify version, and restart comma service.

### v0.9.9 Changes

- `sunnypilot/navd/__init__.py`, `sunnypilot/navd/routing/__init__.py` - Package markers.
- `sunnypilot/navd/routing/osrm_client.py` - OSRM public API client. `get_route(start_lat, start_lon, end_lat, end_lon)` → dict with steps + GeoJSON geometry. No API key required.
- `sunnypilot/navd/routing/geocoder.py` - Geocoding: `autocomplete(query, lat, lon)` via Photon (Komoot), `reverse(lat, lon)` via Nominatim. Both free/keyless.
- `sunnypilot/navd/nav_state.py` - `NavState` class: holds route, tracks step index, computes distance-to-maneuver using Haversine. Detects arrival (<30m) and off-route (>100m). `update(lat, lon)` advances step index. `current_instruction()` returns dict for navInstruction fields.
- `sunnypilot/navd/navigationd.py` - 3 Hz navigation daemon. Watches `NavDestination` param, fetches OSRM route on change, publishes `navInstruction` + `navigationStateSP` cereal messages. Auto re-routes on off-route (10s cooldown). Clears destination on arrival.
- `sunnypilot/navd/nav_webserver.py` - aiohttp server on port 8888. REST API: GET/POST/DELETE `/api/destination`, GET/POST `/api/home`, GET/POST `/api/work`, GET `/api/autocomplete`, GET `/api/status`, GET `/api/gps`. Proxies geocoding. Serves static web UI.
- `sunnypilot/navd/nav_web/index.html` - Single-page web app. Dark theme, Leaflet map, search + autocomplete, Home/Work/Cancel buttons, destination status panel with save-as-home/work.
- `sunnypilot/navd/nav_web/style.css` - Dark theme: `#1a1a2e` background, `#e94560` FunnyPilot red accent. Mobile-first responsive.
- `sunnypilot/navd/nav_web/app.js` - Leaflet map init, debounced autocomplete (300ms), `setDestination()`, `pollStatus()` (3s interval), `cancelNav()`, `saveAsHome/Work()`.
- `selfdrive/ui/sunnypilot/onroad/navigation_panel.py` - `NavigationPanel(Widget)`: bottom-left turn-by-turn overlay (440×100px). Draws directional arrow with raylib primitives based on `maneuverModifier`. Shows distance-to-maneuver, road name, ETA, remaining distance. Hidden when `navigationStateSP.active == False`.
- `selfdrive/ui/sunnypilot/onroad/nav_quick_access.py` - `NavQuickAccess(Widget)`: pre-drive Home/Work/Refresh buttons (3×140×100px, centered bottom). Visible when started but not in Drive. Tapping Home/Work writes `NavDestination` from saved params. Refresh clears+rewrites destination to force re-route.
- `cereal/custom.capnp` - Renamed `CustomReserved10 @0xcb9fd56c7057593a` → `NavigationStateSP` with fields: `active`, `destinationName`, `destinationAddr`, `distanceRemaining`, `timeRemaining`.
- `cereal/services.py` - Added `"navigationStateSP": (True, 1., 10)` to sunnypilot section.
- `selfdrive/ui/sunnypilot/ui_state.py` - Added `"navInstruction"` and `"navigationStateSP"` to `sm_services_ext`.
- `selfdrive/ui/sunnypilot/onroad/hud_renderer.py` - Imported + instantiated `NavigationPanel` and `NavQuickAccess`. Both updated in `_update_state()` and rendered in `_render()`.
- `system/manager/process_config.py` - Added `PythonProcess("navigationd", ...)` and `PythonProcess("nav_webserver", ...)` with `always_run`.

### v0.9.8h Changes (HOTFIX)

- `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` - Reduced max follow distance from 2.5s to 2.25s for standard personality. Aggressive/relaxed updated proportionally (±15%).
- `selfdrive/monitoring/helpers.py` - Driver monitoring tripled again to 9x original (270s passive, 99s active).
- `selfdrive/ui/sunnypilot/onroad/speed_limit.py` - SLA badge moved down 30px to avoid blocking speed limit numbers. Fixed initial display "?0%" → "0%" (removed ± character that rendered incorrectly).
- `selfdrive/ui/sunnypilot/onroad/rocket_fuel.py` - Accel bar shows orange during gas gating (coasting deceleration) instead of red. Reads `vision.gasGating` and `map.gasGating` from `longitudinalPlanSP`.
- `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` - Simplified SCC badge: hidden when inactive, red when active braking, orange when gas gating. Removed pulsing, green color, and GAS GATE sub-label.
- `selfdrive/controls/lib/longitudinal_planner.py` - Replaced flat 70% accel cap with speed-dependent taper: full accel 0–25mph, linear 1%/mph reduction 25–75mph, capped at 50% above 75mph.
- `selfdrive/car/cruise.py`, `sunnypilot/selfdrive/car/cruise_ext.py`, `opendbc_repo/opendbc/car/interfaces.py` - Raised `V_CRUISE_MAX` from 145 kph (90mph) to 210 kph (130mph). `MAX_CTRL_SPEED` set independently to 153 kph (~95mph) for silent high-speed banner.
- `selfdrive/controls/lib/longitudinal_planner.py` - Brake jerk limiter: rate-limits decel-direction changes to 4 m/s³ to eliminate lurch when MPC first acquires a lead (accel side unrestricted). Lead loss coast grace period: holds last lead speed as v_cruise cap for 2.5s then releases over 2.0s on lead disappearance, preventing accelerate-then-brake cycle. Both reset cleanly on disengage.
- Removed Tailscale from device (PUSH098h.sh includes removal steps: stops/disables systemd service, removes `/data/tailscale` and service drop-in). SSH now home network only.

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
