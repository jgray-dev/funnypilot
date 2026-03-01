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
