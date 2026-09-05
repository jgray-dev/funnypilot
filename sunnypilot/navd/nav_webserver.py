"""
FunnyPilot terminal server — PTY-backed bash shell over WebSocket at /ws.
Branch selector sorted by last commit date (most recent first).
POST /api/flash  — hard-sets device to chosen funnypilot branch and reboots.
GET  /api/branches — list funnypilot branches, newest first.
"""
import asyncio
import hashlib
import json
import os
import pty
import fcntl
import re
import termios
import time
import struct
import concurrent.futures
import functools
import tarfile

import aiohttp
from aiohttp import web

from openpilot.sunnypilot.navd import drive_index

# ── logging, deliberately lazy (v3.6.9) ─────────────────────────────────────
#
# v3.6.8 imported `openpilot.common.swaglog` AT MODULE SCOPE and that was a
# mistake in a process on the startup path. Importing it is not cheap or inert:
# it builds a rotating file handler that `os.listdir`s `/data/log` (thousands
# of files, `backup_count=2500`) and calls `doRollover()` right there, and it
# stands up a zmq PUSH handler in a process that later `pty.fork()`s for the
# terminal — the fork-with-a-zmq-context hazard openpilot's own source has a
# TODO about.
#
# None of that may happen before the port is listening, and none of it is
# needed unless something actually goes wrong. So it is imported ON FIRST LOG,
# cached, and degraded to stdlib logging if it fails. A dashboard that cannot
# log is still a dashboard; one that cannot finish importing is a restart loop.
_CLOUDLOG = None


def _log():
  global _CLOUDLOG
  if _CLOUDLOG is None:
    try:
      from openpilot.common.swaglog import cloudlog as _c
    except Exception:                                    # pragma: no cover
      import logging
      _c = logging.getLogger("nav_webserver")
    _CLOUDLOG = _c
  return _CLOUDLOG

REPO = "jgray-dev/funnypilot"
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "nav_web")
_SHELL = "/bin/bash"
_PORT = 8888
_VERSION_FILE = "/data/openpilot/FUNNYPILOT_VERSION"

# FunnyPilot v3.2.7: triage flight-recorder logs (see
# selfdrive/controls/lib/triage_recorder.py for the format and rationale).
TRIAGE_DIR = "/data/funnypilot_triage"
_TRIAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(jsonl|jsonl\.1|log)$")
# a git ref name we are willing to hand to a root shell
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PULSE_PERIOD_S = 600  # code-identity pulse every 10 min, catches mid-parked swaps
_PULSE_MAX_BYTES = 1024 * 1024
# files whose on-disk content defines the "smoothing" feel — hashed each pulse
_FEEL_FILES = [
  "/data/openpilot/selfdrive/controls/lib/lat_smooth.py",
  "/data/openpilot/selfdrive/controls/lib/knot_filter.py",
  "/data/openpilot/selfdrive/controls/lib/lat_handback.py",
  "/data/openpilot/selfdrive/controls/lib/steering_motion.py",
  "/data/openpilot/selfdrive/controls/lib/eps_limit.py",
  "/data/openpilot/selfdrive/controls/lib/bump_damper.py",
  "/data/openpilot/selfdrive/controls/lib/long_shaping.py",
  "/data/openpilot/selfdrive/controls/lib/turn_limit.py",
  "/data/openpilot/selfdrive/controls/lib/lead_physics.py",
  "/data/openpilot/selfdrive/controls/controlsd.py",
  "/data/openpilot/selfdrive/controls/lib/latcontrol_torque.py",
  "/data/openpilot/selfdrive/controls/lib/latcontrol_pid.py",
  "/data/openpilot/selfdrive/controls/lib/longcontrol.py",
  "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py",
  "/data/openpilot/sunnypilot/selfdrive/controls/lib/latcontrol_torque_v0.py",
  "/data/openpilot/sunnypilot/selfdrive/controls/lib/latcontrol_torque_ext.py",
  "/data/openpilot/sunnypilot/selfdrive/controls/lib/latcontrol_torque_ext_base.py",
  "/data/openpilot/sunnypilot/selfdrive/controls/lib/nnlc/nnlc.py",
  "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud_renderer.py",
  "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/tokens.py",
]

# Expected version for the running branch (used by /api/diagnostics).
EXPECTED_VERSION = "3.7.1a"

# FunnyPilot v3.5.8 — FLASH-TIME HOUSEKEEPING.
#
# Both of these run ONLY after a checkout has actually succeeded, and only from
# the explicit web flash. Neither is reachable from a background updater.
#
# BRANCH PRUNE. Every version ever flashed used to accumulate as a local branch.
# The stated reason was an offline unbrick path -- check out an old branch
# straight from local git objects with no network. That reason does not survive
# contact with the device: reaching the CLI at all requires wifi or a hotspot,
# so if you can run git you can also fetch. Versions ending in `st` are the
# stable cuts and are kept; everything else goes, along with the disk it holds.
KEEP_BRANCH_SUFFIX = "st"

# DRIVE DATA. v3.5.8 set this True because realdata was 60-70 GB of route
# segments with NO CONSUMER -- no uploader, no viewer -- and left an explicit
# instruction: "FLIP THIS TO False once the dashcam viewer exists; at that point
# the data has a reader and deleting it on every flash becomes destructive
# rather than tidy."
#
# v3.6.8 IS THAT MOMENT. The Drives section of this dashboard plays, charts and
# downloads exactly these segments, so a flash that emptied realdata would now
# delete the thing the feature exists to show -- and it would do it silently,
# in the tail of a command whose visible job is to change branches.
#
# STORAGE IS STILL BOUNDED, JUST NOT BY THIS. `deleter.py` holds free space at
# its 5 GB / 10% floor by evicting the oldest segments, and the dashboard has a
# per-drive delete with the sizes shown next to it -- so the disk is managed by
# something the owner can see and steer, rather than by a side effect of
# flashing.
PURGE_DRIVE_DATA_ON_FLASH = False
_DRIVE_DATA_DIR = "/data/media/0/realdata"

# FunnyPilot v3.3.3: the Verify list is CONSOLIDATED — one row per question
# the user actually asks ("is my code intact / will it stay that way"),
# instead of one row per historical grep. All commands are non-mutating.
# `-c safe.directory=*` avoids git "dubious ownership" failures when the
# webserver uid differs from the checkout owner.
_GIT = "git -c safe.directory='*' -C /data/openpilot"

# Every load-bearing FunnyPilot marker, verified in ONE check. Each entry:
# (grep pattern, file, short label). A missing marker means the on-disk code
# is not the shipped branch — the single "code" row fails and names it.
_CODE_MARKERS = [
  ("class LatSmoother", "/data/openpilot/selfdrive/controls/lib/lat_smooth.py", "lat knot smoother"),
  ("SPLINE", "/data/openpilot/selfdrive/controls/lib/lat_smooth.py", "C1 spline shaping"),
  ("class EpsTorqueGovernor", "/data/openpilot/selfdrive/controls/lib/eps_limit.py", "EPS torque governor"),
  ("v3.3.8", "/data/openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py", "MPC blended mode restore"),
  ("v3.3.6", "/data/openpilot/selfdrive/controls/controlsd.py", "controlsd smoother wiring"),
  ("v3.3.2", "/data/openpilot/sunnypilot/modeld_v2/modeld.py", "EMA smoothing revert"),
  ("_OVERRIDE_MIN_SCALE", "/data/openpilot/selfdrive/controls/lib/latcontrol_torque.py", "override softening"),
  ("class OverrideGate", "/data/openpilot/selfdrive/controls/lib/override_gate.py", "override gate"),
  ("v3.2.3st", "/data/openpilot/opendbc_repo/opendbc/car/hyundai/carcontroller.py", "brake chime fix"),
  ("UNWIND_SETTLE_TIME", "/data/openpilot/sunnypilot/selfdrive/controls/lib/blinker_pause_lateral.py", "blinker unwind"),
  ("class AccelJerkShaper", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "long output shaper"),
  ("v3.3.3", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "long planner + SLA gas gate"),
  ("class CurveSpeedCap", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/curve_cap.py", "SCC v2 curve cap"),
  ("v3.3.3", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "SLA arrow activation"),
  ("v3.3.0e", "/data/openpilot/opendbc_repo/opendbc/car/hyundai/values.py", "K5 radar tracks flag"),
  ("radar_enable.jsonl", "/data/openpilot/opendbc_repo/opendbc/sunnypilot/car/hyundai/enable_radar_tracks.py", "verified radar enable"),
  ("FINALIZED_BRANCH", "/data/openpilot/launch_chffrplus.sh", "boot branch guard"),
  ("adopting flashed branch", "/data/openpilot/system/updated/updated.py", "updater self-heal"),
  ("class TriageRecorder", "/data/openpilot/selfdrive/controls/lib/triage_recorder.py", "triage recorder"),
  ("HIDDEN_CRUISE_OFFSET", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "hidden cruise governor"),
  ("gate_map_target", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/speed_governor.py", "SCC map/vision arbitration"),
  ("class BumpDamper", "/data/openpilot/selfdrive/controls/lib/bump_damper.py", "bump/weight-transfer error damper"),
  ("_update_cruise_ramp", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "SLA predictive set-speed ramp"),
  ("class LongStatusDotRenderer", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/long_status_dot.py", "long command status dot"),
  ("class AutoUpdater", "/data/openpilot/sunnypilot/auto_updater/manager.py", "offroad wifi auto-updater"),
  ("def write_sla_shm", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/sla_shm.py", "SLA /dev/shm channel"),
  ("deliberately UNANNOTATED", "/data/openpilot/sunnypilot/selfdrive/car/cruise_ext.py", "capnp union boot fix (v3.4.2)"),
  ("class BrakeLightPublisher", "/data/openpilot/sunnypilot/selfdrive/car/brake_light_shm.py", "brake-lamp /dev/shm channel"),
  # plain substring: the marker string is fed to grep inside single quotes, so
  # keep it free of quotes/metacharacters
  ("BrakeLight", "/data/openpilot/opendbc_repo/opendbc/sunnypilot/car/hyundai/carstate_ext.py", "brake-lamp bit read from CAN"),
  ("def cleanup_async", "/data/openpilot/system/manager/storage_cleanup.py", "startup storage cleanup"),
  ("storage_cleanup.cleanup_async", "/data/openpilot/system/manager/manager.py", "storage cleanup wired into manager"),
  # v3.4.5: the clock-domain fix is the load-bearing part of the predictive
  # ramp — without it distance_to_next_limit reads ~4e10 m and every consumer
  # of it is silently dead code on a moving car.
  ("MAP_MSG_MAX_AGE", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_resolver.py", "map-data clock-domain fix"),
  ("RAMP_ARRIVE_EARLY_T", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "predictive set-speed ramp (v3.4.5)"),
  ("BUTTON_INTENT_FRAMES", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "button-intent ratio gate"),
  ("STALE_S", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/sla_shm.py", "SLA shm staleness gate"),
  ("GATE_V_MARGIN", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "gas gate v_ego term (v3.4.8)"),
  ("GATE_MAX_FRAMES", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "gas gate watchdog"),
  ("ENGAGE_GRACE_FRAMES", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "ramp dropout hysteresis"),
  ("RAMP_UP_T_MAX", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "time-based up-ramp window"),
  # v3.4.9
  ("class KnotFilter", "/data/openpilot/selfdrive/controls/lib/knot_filter.py", "predictive knot damping"),
  ("knot_filter", "/data/openpilot/selfdrive/controls/controlsd.py", "knot filter wired into controlsd"),
  ("class LatHandback", "/data/openpilot/selfdrive/controls/lib/lat_handback.py", "divergence-scheduled handback"),
  ("_handback", "/data/openpilot/selfdrive/controls/lib/latcontrol_torque.py", "handback owns the override scale"),
  ("RAMP_DISPLACED_TH", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "SLA driver adjust is a delta"),
  ("v_cruise_target", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "SLA publishes the ramp target"),
  ("def fuse_map_target", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_fusion.py", "merged SCC arbitration"),
  ("corroboration", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_vision_v2.py", "SCC-V corroboration signal"),
  # v3.5.0
  ("def write_scc_shm", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_shm.py", "SCC governing-point channel"),
  ("def safe_draw", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/tokens.py", "onroad HUD blast shield"),
  ("def draw_state_glow", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/chrome.py", "state edge glow"),
  ("class RouteMap", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "SCC-M route minimap"),
  ("def halo_spec", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/speed_sign.py", "text-free SLA sign halo"),
  ("HORIZON BANDS", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud_renderer.py", "horizon-band HUD layout"),
  # v3.5.0 SCC-Learn
  ("class LearnStore", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_learn_store.py", "learned-corner store"),
  # v3.5.1
  ("def _spawn_write", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_learn_store.py", "store writes off the planner thread"),
  ("def edge_fade", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap edge fade"),
  ("POSE_TAU", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap pose smoothing"),
  ("INFORMATIONAL BANNERS ARE SUPPRESSED", "/data/openpilot/selfdrive/ui/onroad/alert_renderer.py", "info banner filter"),
  # v3.5.2
  ("def expected_speed_at", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap tint vs expected speed"),
  ("def _draw_ego", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap ego marker"),
  # v3.5.3
  ("self.prev_accel_clip = list", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "accel clip reset on disengage"),
  ("JERK_DOWN_BP = [0.0, 1.0]", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "gentle partial throttle release"),
  # v3.5.4
  ("def predicted_lat_accel", "/data/openpilot/selfdrive/controls/lib/turn_limit.py", "anticipatory turn limiting"),
  ("def starting_accel_rate", "/data/openpilot/selfdrive/controls/lib/longcontrol.py", "scheduled creep launch"),
  ("class Eased", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/tokens.py", "one house easing primitive"),
  ("def chrome_scale", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/chrome.py", "scene-adaptive chrome"),
  ("def long_dot_color", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/stations.py", "cross-faded long dot"),
  # v3.5.5
  ("def believed_lead_decel", "/data/openpilot/selfdrive/controls/lib/lead_physics.py", "lead stopping-distance physics"),
  ("def lateral_offset_at_ego", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap lane alignment"),
  ("def stitch_to_ego", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap ribbon reaches the car"),
  # v3.6.3 REVERSED v3.5.5's escape hatch on purpose, so this row moved with it:
  # the marker used to grep "INACTIVE WAS A TRAP", which that release deleted,
  # and the Verify page has been reporting MISSING ever since.
  ("THE WINDOW IS THE WHOLE PERMISSION",
   "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py",
   "SLA activates only inside its window"),
  # v3.5.6
  ("def proximity_authority", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_fusion.py", "SCC-M proximity authority"),
  ("DECIMATE_M", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap route decimation"),
  ("def _rings", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/chrome.py", "chrome ring cache"),
  ("def zone_change", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap zone-boundary marker"),
  # v3.5.7
  ("power watchdog not kicked", "/data/openpilot/system/manager/manager.py", "AGNOS watchdog failure is logged"),
  # v3.5.8
  ("PURGE_DRIVE_DATA_ON_FLASH", "/data/openpilot/sunnypilot/navd/nav_webserver.py", "flash drive-data policy"),
  # v3.6.8
  ("def _safe_segment_dir", "/data/openpilot/sunnypilot/navd/drive_index.py", "drive path validator"),
  ("def hls_playlist", "/data/openpilot/sunnypilot/navd/drive_index.py", "playback without transcoding"),
  # v3.6.9
  ("app[\"triage_boot\"]", "/data/openpilot/sunnypilot/navd/nav_webserver.py", "port opens before housekeeping"),
  ("def _nice_worker", "/data/openpilot/sunnypilot/navd/nav_webserver.py", "executor initializer cannot break the pool"),
  # v3.7.0
  ("def follow_line_segment", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/follow_line.py", "follow-distance hologram geometry"),
  ("def write_follow_shm", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_shm.py", "planner publishes the held gap"),
  ("_follow_gap_m", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "held gap = t_follow*v + STOP_DISTANCE"),
  ("TURN_HOLD_S", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "a turn abandons the pass"),
  ("JUNCTION_K", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "junction takeover is a turn-off"),
  ("demo and target > hi", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_speed.py", "a demonstration lifts the ceiling"),
  ("STOP_GOV_DECEL_MIN", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "stop governor acts on a braking lead"),
  ("STARTING_UPPER_JERK", "/data/openpilot/opendbc_repo/opendbc/sunnypilot/car/hyundai/longitudinal/controller.py", "launch jerk allowance"),
  ("KEEP_BRANCH_SUFFIX", "/data/openpilot/sunnypilot/navd/nav_webserver.py", "flash prunes non-stable branches"),
  # v3.5.9
  ("MODEL_HORIZON_T", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_fusion.py", "SCC-M vision-disagreement veto"),
  ("def screen_opacity", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap opacity profile"),
  ("def draw_status_stack", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/stations.py", "vertical pill stack"),
  # v3.6.0
  ("def _fit_label", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/stations.py", "pill label fits its slot"),
  ("def _why", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap logs why it is empty"),
  # v3.6.1
  ("OSM_MIN_REFRESH_S", "/data/openpilot/sunnypilot/auto_updater/manager.py", "OSM refresh rate-limited"),
  # v3.6.2 — SCC-M v2: corner radius measured from the drawn route, lateral
  # budget learned from how this car actually drives each bend.
  ("MIN_CORNER_TURN_DEG", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/road_geometry.py", "corner detector rejects noisy straights"),
  ("def curvature_profile", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/road_geometry.py", "radius from turn angle over arc length"),
  ("def smooth_polyline", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/road_geometry.py", "polyline smoothing removes node aliasing"),
  ("def effective_a_lat", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_speed.py", "learned budget blended by confidence"),
  ("def update_interval", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_speed.py", "the learned lateral-accel interval"),
  ("_J_BP", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_speed.py", "integrated approach budget"),
  ("class LateralEffort", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "oscillation / clamp / saturation"),
  ("def observe_frame", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "SCC-M v2 learns every pass"),
  ("def write_corners_shm", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_shm.py", "corner list published to the minimap"),
  ("def corner_speed_at", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap tints by our own corner speeds"),
  ("def update_car_state_sp", "/data/openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py", "SCC-M v2 wired at the carState rate"),
  ("def _update_gas_gate", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "SCC-M v2 anticipatory gas gate"),
  ("gas_gating_active", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "the gas gate reaches the throttle clip"),
  ("def window_around_ego", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/road_geometry.py", "forward horizon measured from the car"),
  ("def write_scc_debug_shm", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_shm.py", "dev UI payload channel"),
  ("class LongSourceElement", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py", "SRC dev-UI readout"),
  ("class SideSignal", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/side_signals.py", "blinker + blind spot on the edges"),
  ("def draw_side_signal", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/chrome.py", "side glow band"),
  # v3.6.4
  ("def lane_departure_m", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "lane departure severity signal"),
  ("MIN_ENGAGED_FRAC", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "only openpilot's own passes are learned"),
  ("def plan_alpha", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "minimap tint is the long-control plan"),
  # v3.6.5
  ("def corner_cap", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_speed.py", "corner run-out hands throttle back"),
  # v3.7.1
  ("EXIT_LAT_FRAC", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_speed.py", "corner exit ramp from the apex"),
  ("def path_opening", "/data/openpilot/selfdrive/controls/lib/turn_limit.py", "turn limit exit allowance"),
  ("STORE_ROUTE_MAX_M", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "store records must lie on the route"),
  ("BEHIND_MAX_M", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "corners listed through their run-out"),
  ("_v_scc_map_gated", "/data/openpilot/sunnypilot/selfdrive/controls/lib/longitudinal_planner.py", "MAP pill reads the gated cap"),
  ("def tint_for", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/follow_line.py", "guide bar white-to-red tint"),
  ("def _dead_reckon", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "corner distances close between refreshes"),
  ("TAKEOVER_SEVERITY", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "a driver takeover is a verdict"),
  ("y POSITIVE RIGHT", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "lane departure frame convention fixed"),
  # v3.6.5, second pass
  ("MIN_DEMO_S", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "gas with lateral on is a demonstration"),
  ("DEPART_DEADBAND_M", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "lane departure deadband + low pass"),
  ("def _observe_orphan", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "bends the geometry never listed are learned"),
  ("def _corners_from_store", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "learned bends enter the corner list"),
  ("def is_unmanageable", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_map_v2.py", "bends the cap cannot fix"),
  ("def read_corner_warning_shm", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_shm.py", "preemptive curve warning channel"),
  ("class LongTrackingElement", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py", "TRK dev-UI readout"),
  ("class SccCorroborationElement", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py", "CORR dev-UI readout"),
  ("def long_source_code", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "SRC classified by the planner"),
  ("BOTTOM_ROWS", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/developer_ui/__init__.py", "two-row dev panel"),
  # v3.6.6
  ("class StopGovernor", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "stop-and-go lead speed cap"),
  ("self.stop_gov.update", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "stop governor reaches v_cruise"),
  ("CRUISE_MIN_ACCEL = -2.0", "/data/openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py", "corner decel headroom"),
  ("allow_lower", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_learn_store.py", "a manual speed only ever raises"),
  ("long_manual", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/corner_effort.py", "longitudinal handover is not a takeover"),
  ("KNOWN_FREE_RGB", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud/route_map.py", "green for a bend that costs nothing"),
  ("ALWAYS DRAWN", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud_renderer.py", "minimap survives lateral-only"),
  ("_corner_warn", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/hud_renderer.py", "curve warning is an edge pulse"),
  # v3.6.7
  ("def _update_command_rate", "/data/openpilot/opendbc_repo/opendbc/sunnypilot/car/hyundai/longitudinal/controller.py", "predictive tuning tracks a ramp"),
  ("STOP_GOV_CLOSE_ON", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "stop governor only acts while closing"),
  ("VISION_DISAGREE_TH = 0.30", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/scc_fusion.py", "vision outranks the map"),
  ("vision_available", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/speed_governor.py", "the veto knows if SCC-V is running"),
  # v3.7.1a
  ("def prepare_pid", "/data/openpilot/sunnypilot/selfdrive/controls/lib/nnlc/nnlc.py", "one PID update in the selected units"),
  ("self._freeze_integrator = freeze_integrator", "/data/openpilot/sunnypilot/selfdrive/controls/lib/latcontrol_torque_ext.py", "neural handback freeze"),
  ("self.shaper.reset(self.output_a_target)", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "shaper follows published acceleration"),
  ("a_target <= min(self.a, 0.0)", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "planned braking bypasses comfort ramp"),
  ("min(self.last_output_accel, a_target, 0.0)", "/data/openpilot/selfdrive/controls/lib/longcontrol.py", "stopping preserves planned braking"),
  ("class SteeringMotionCredit", "/data/openpilot/selfdrive/controls/lib/steering_motion.py", "signed wheel-motion credit"),
  ("error *= motion_scale", "/data/openpilot/selfdrive/controls/lib/latcontrol_torque.py", "motion credit reaches error feedback"),
  ("credit / abs(neural_error)", "/data/openpilot/sunnypilot/selfdrive/controls/lib/nnlc/nnlc.py", "neural credit bounded across reference horizons"),
]
_CODE_CMD = "; ".join(
  f"grep -qs '{pat}' '{path}' && echo 'ok       {label}' || echo 'MISSING  {label}'"
  for pat, path, label in _CODE_MARKERS
)

# Updater state in one row: the target branch (what a background fetch would
# stage) and any already-staged update. Mismatches here are the root cause of
# the historical "reverts after sitting parked" issue.
_UPDATER_CMD = (
  "echo \"target: $(cat /data/params/d/UpdaterTargetBranch 2>/dev/null || echo '(unset)')\"; " +
  "echo \"staged: $(git -c safe.directory='*' -C /data/safe_staging/finalized rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(none)')\""
)

# FunnyPilot v3.4.5: READ-ONLY storage picture. The "storage full" report on
# the v3.4.4 flash could not be diagnosed because nothing recorded what was
# actually consuming the disk — this row exists so the next occurrence is
# evidence rather than a guess. `du -sh` on a handful of named suspects only;
# no whole-filesystem walk (that would take minutes on the drive log tree).
_STORAGE_CMD = (
  "df -h /data | tail -1; " +
  "du -sh /data/media/0/realdata /data/openpilot/.git /data/safe_staging/old_openpilot " +
  "/data/core /data/funnypilot_triage 2>/dev/null"
)

DIAG_CHECKS = [
  {"id": "version", "name": "FunnyPilot version",       "cmd": "cat /data/openpilot/FUNNYPILOT_VERSION 2>&1"},
  {"id": "branch",  "name": "Git branch",               "cmd": f"{_GIT} rev-parse --abbrev-ref HEAD 2>&1"},
  {"id": "clean",   "name": "Working tree unmodified",  "cmd": f"{_GIT} status --porcelain 2>&1"},
  {"id": "code",    "name": "Shipped code markers",     "cmd": _CODE_CMD},
  {"id": "updater", "name": "Updater target / staged",  "cmd": _UPDATER_CMD},
  {"id": "model_bundle", "name": "Active model bundle", "cmd": "cat /data/params/d/ModelManager_ActiveBundle 2>/dev/null || echo '(none / stock)'"},
  {"id": "logs",    "name": "Triage logs on disk",      "cmd": "ls -sh1 /data/funnypilot_triage/ 2>/dev/null || echo '(no logs yet)'"},
  {"id": "storage", "name": "Disk usage",               "cmd": _STORAGE_CMD},
]


def _eval_diag(check_id: str, out: str):
  """Return (level, summary) for a check. level in pass|fail|warn|info."""
  s = out.strip()
  if check_id == "version":
    return ("pass", s) if s == EXPECTED_VERSION else ("warn", f"{s or '(empty)'} (expected {EXPECTED_VERSION})")
  if check_id == "branch":
    return ("pass" if EXPECTED_VERSION in s else "warn", s or "(unknown)")
  if check_id == "clean":
    return ("pass", "clean") if s == "" else ("fail", "MODIFIED")
  if check_id == "code":
    missing = [ln.split(None, 1)[1] for ln in s.splitlines() if ln.startswith("MISSING")]
    if missing:
      return ("fail", f"MISSING: {', '.join(missing)}")
    return ("pass", f"all {len(_CODE_MARKERS)} markers present")
  if check_id == "updater":
    # A target that differs from the flashed branch is exactly what caused the
    # "reverts after sitting offroad" issue — the updater stages that branch.
    target = staged = ""
    for ln in s.splitlines():
      if ln.startswith("target:"):
        target = ln[len("target:"):].strip()
      elif ln.startswith("staged:"):
        staged = ln[len("staged:"):].strip()
    if target not in ("", "(unset)") and EXPECTED_VERSION not in target:
      return ("fail", f"updater targets a DIFFERENT branch: {target}")
    if staged not in ("", "(none)") and EXPECTED_VERSION not in staged:
      return ("warn", f"staged: {staged} (boot guard will discard it)")
    return ("pass", f"target {target or '(unset)'}, staged {staged or '(none)'}")
  if check_id == "storage":
    # First line is `df -h /data | tail -1`; its 5th field is use%. Graded, not
    # merely reported, so "storage full" shows up here BEFORE it shows up as a
    # failed flash. old_openpilot appearing at all is its own warning: the
    # startup cleanup should have removed it, and while it exists
    # launch_chffrplus.sh refuses to install any update.
    lines = s.splitlines()
    use = ""
    if lines:
      parts = lines[0].split()
      use = next((p for p in parts if p.endswith("%")), "")
    stale_backup = any("old_openpilot" in ln for ln in lines)
    try:
      pct = int(use.rstrip("%"))
    except ValueError:
      return ("info", s.replace("\n", " | ") or "(unavailable)")
    if pct >= 95:
      return ("fail", f"/data {use} full")
    if stale_backup:
      return ("warn", f"/data {use} used; old_openpilot backup present (blocks updates)")
    if pct >= 85:
      return ("warn", f"/data {use} used")
    return ("pass", f"/data {use} used")
  return ("info", "")


async def _run_diag_check(check: dict) -> dict:
  try:
    proc = await asyncio.create_subprocess_shell(
      check["cmd"],
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    text = out_b.decode(errors="replace")
  except TimeoutError:
    text = "(timed out)"
  except Exception as e:
    text = f"(error: {e})"
  level, summary = _eval_diag(check["id"], text)
  return {"id": check["id"], "name": check["name"], "cmd": check["cmd"],
          "output": text.rstrip(), "level": level, "summary": summary}


async def handle_diagnostics(request: web.Request) -> web.Response:
  try:
    results = await asyncio.gather(*[_run_diag_check(c) for c in DIAG_CHECKS])
    return web.json_response({"checks": list(results)})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


# ── branch ordering (v3.7.0) ────────────────────────────────────────────────
#
# THE LIST IS SORTED BY VERSION, NOT BY COMMIT DATE, and the reason is worth
# recording. The first version fetched every branch's commit to read its date
# and sorted on that. Unauthenticated GitHub allows 60 requests an hour; with
# this many branches, two opens of the flash modal exhaust it, a rate-limited
# date fetch returns "", and "" sorts to the BOTTOM of a descending sort. So
# whichever branch happened to be refused sank — reported as 3.7.0, the newest,
# at the foot of the list. The order was not wrong, it was random.
#
# Version order is the owner's own convention, is deterministic, and costs no
# requests at all. Dates are still fetched — best-effort, for the FIRST few
# rows only, and never as a sort key — because they are useful to read and
# bounded at DATE_FETCH_N requests the limit can comfortably absorb.
_VER_RE = re.compile(r"^funnypilot-(\d+)\.(\d+)\.(\d+)([a-z]*)$")
DATE_FETCH_N = 12


def version_key(name: str):
  """A sort key that puts the newest FunnyPilot branch first under `reverse=True`.

  `funnypilot-3.7.0` > `funnypilot-3.6.9`, and a suffixed cut sorts newer than
  its bare version (`3.2.3st` is a stable cut MADE AFTER 3.2.3). Anything that
  does not match the convention sorts below everything that does, then
  alphabetically, so a stray branch can never displace a real release.
  """
  m = _VER_RE.match(name or "")
  if not m:
    return (0, 0, 0, 0, name or "")
  major, minor, patch, suffix = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
  # The suffix string alone orders a bare version below its cuts: "" sorts
  # below any non-empty string. A first draft also carried a `1 if suffix else
  # 0` element, and a mutation that zeroed it survived every test — because it
  # was EQUIVALENT, not because a guard was missing. It is gone rather than
  # guarded; the v3.6.2 rule is to record an equivalence, not manufacture a
  # test for a term that cannot change an answer.
  return (1, major, minor, patch, suffix)


def sort_branches(names):
  """Newest release first. Pure; the endpoint and the tests share it."""
  return sorted(names, key=version_key, reverse=True)


async def _fetch_branches(session: aiohttp.ClientSession):
  branches = []
  page = 1
  while True:
    url = f"https://api.github.com/repos/{REPO}/branches?per_page=100&page={page}"
    async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as resp:
      if resp.status != 200:
        break
      data = await resp.json()
      if not data:
        break
      branches.extend(data)
      if len(data) < 100:
        break
      page += 1

  fp = {b["name"]: b for b in branches if b["name"].startswith("funnypilot-")}
  ordered = sort_branches(fp.keys())

  async def get_date(name):
    try:
      async with session.get(fp[name]["commit"]["url"],
                             headers={"Accept": "application/vnd.github+json"}) as r:
        if r.status == 200:
          c = await r.json()
          return c.get("commit", {}).get("committer", {}).get("date", "")
    except Exception:
      pass
    return ""

  # Dates for the head of the list only, and they decorate — they do not sort.
  head = ordered[:DATE_FETCH_N]
  dates = dict(zip(head, await asyncio.gather(*[get_date(n) for n in head]), strict=True))
  return [{"name": n, "date": dates.get(n, "")} for n in ordered]


async def handle_branches(request: web.Request) -> web.Response:
  try:
    async with aiohttp.ClientSession() as session:
      branches = await _fetch_branches(session)
    # v3.6.9 — a NAMED FIELD, not a bare array. The v3.6.8 dashboard read
    # `d.branches` off a top-level list and threw
    # "Cannot read properties of undefined (reading 'slice')". `branches` is
    # kept as the response's own name so the client and the server agree by
    # construction; the client still tolerates the old bare array, because the
    # two are separate files and a half-updated device should degrade rather
    # than break.
    return web.json_response({"branches": branches, "count": len(branches)})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_flash(request: web.Request) -> web.Response:
  try:
    body = await request.json()
    branch = body.get("branch", "").strip()
    if not branch:
      return web.json_response({"error": "missing branch"}, status=400)
    # Only allow funnypilot branches or sunnypilot main/dev/staging
    allowed = branch.startswith("funnypilot-") or branch in ("main", "dev", "staging")
    # v3.5.8: `branch` is interpolated into a shell command that runs as root,
    # and the prefix test alone does not make it safe -- "funnypilot-;<anything>"
    # passes startswith(). Restrict to characters a git ref can actually contain.
    if not _BRANCH_RE.match(branch):
      allowed = False
    if not allowed:
      return web.json_response({"error": "disallowed branch"}, status=403)

    # Keep the stock updater aligned with the explicit flash: point its target
    # at the flashed branch so it can never stage (and boot-swap in) a stale
    # branch after an offroad fetch. Best-effort — the launch_chffrplus.sh
    # branch guard and the updated.py self-heal are the real backstops.
    try:
      from openpilot.common.params import Params
      Params().put("UpdaterTargetBranch", branch)
    except Exception:
      pass

    # After a successful checkout, also discard any previously staged update
    # (unmount the updater overlay first) so the reboot below can't swap in
    # code that was finalized before this flash.
    # v3.5.8 housekeeping, in the tail block so it runs ONLY after the fetch,
    # checkout and reset have all succeeded -- a failed flash must never delete
    # anything.
    #
    # The prune keeps `*st` and the branch just flashed. `grep -vx` on the
    # flashed branch is load-bearing: git refuses to delete the checked-out
    # branch anyway, but relying on that would make the pipeline's exit status
    # depend on it.
    prune = (
      "sudo git for-each-ref --format='%(refname:short)' refs/heads" +
      f" | grep -vE '{KEEP_BRANCH_SUFFIX}$' | grep -vx '{branch}'" +
      " | xargs -r sudo git branch -D; "
    )
    # -mindepth 1 keeps the directory itself, which loggerd expects to exist,
    # and `-exec ... +` avoids a glob that would blow ARG_MAX on a device
    # holding tens of thousands of segments.
    purge = (
      f"sudo find {_DRIVE_DATA_DIR} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + 2>/dev/null; "
    ) if PURGE_DRIVE_DATA_ON_FLASH else ""

    script = (
      "cd /data/openpilot && " +
      f"sudo git -c http.sslVerify=false fetch funnypilot {branch} && " +
      f"sudo git checkout {branch} && " +
      f"sudo git reset --hard funnypilot/{branch} && " +
      "{ " + prune + purge +
      "sudo umount -l /data/safe_staging/merged 2>/dev/null; " +
      "sudo rm -rf /data/safe_staging; " +
      # The git calls above run under sudo and leave root-owned objects behind;
      # that is what made three previous deploys abort half-way with
      # "insufficient permission for adding an object". Hand the tree back.
      "sudo chown -R comma:comma /data/openpilot; " +
      "sudo reboot; }"
    )
    proc = await asyncio.create_subprocess_exec(
      "/bin/bash", "-c", script,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    asyncio.ensure_future(proc.wait())
    return web.json_response({"status": "flashing", "branch": branch})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  master_fd, slave_fd = pty.openpty()

  env = dict(os.environ)
  env.pop("PYTHONHOME", None)
  venv_path = env.pop("VIRTUAL_ENV", None)

  if venv_path:
    path_entries = [p for p in env.get("PATH", "").split(":") if p and not p.startswith(venv_path)]
    if path_entries:
      env["PATH"] = ":".join(path_entries)
    else:
      env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
  env.setdefault("HOME", os.environ.get("HOME", "/root"))
  env["TERM"] = "xterm-256color"

  proc = await asyncio.create_subprocess_exec(
    _SHELL, "--login",
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    cwd="/data/openpilot",
    close_fds=True,
    env=env,
  )
  os.close(slave_fd)

  if venv_path:
    try:
      os.write(master_fd, b"deactivate\n")
    except OSError:
      pass

  loop = asyncio.get_event_loop()

  async def pty_reader():
    try:
      while not ws.closed:
        try:
          data = await loop.run_in_executor(None, lambda: os.read(master_fd, 4096))
          if not data:
            break
          await ws.send_bytes(data)
        except OSError:
          break
    finally:
      if not ws.closed:
        await ws.close()

  reader_task = asyncio.ensure_future(pty_reader())

  try:
    async for msg in ws:
      if msg.type == aiohttp.WSMsgType.BINARY:
        data = msg.data
        if len(data) > 1 and data[0] == 0xFF:
          # Resize packet: 0xFF + 4 bytes (cols, rows as uint16 LE each)
          if len(data) >= 5:
            cols = data[1] | (data[2] << 8)
            rows = data[3] | (data[4] << 8)
            try:
              winsize = struct.pack("HHHH", rows, cols, 0, 0)
              fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
              pass
        else:
          try:
            os.write(master_fd, data)
          except OSError:
            break
      elif msg.type == aiohttp.WSMsgType.TEXT:
        try:
          os.write(master_fd, msg.data.encode())
        except OSError:
          break
      elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
        break
  finally:
    reader_task.cancel()
    try:
      proc.kill()
    except Exception:
      pass
    try:
      os.close(master_fd)
    except OSError:
      pass
    await proc.wait()

  return ws


# ---------------------------------------------------------------------------
# FunnyPilot v3.2.7 triage: code-identity snapshots + log access for the web UI
# ---------------------------------------------------------------------------

async def _sh(cmd: str, timeout: float = 10) -> str:
  try:
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
    out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return out_b.decode(errors="replace").strip()
  except Exception as e:
    return f"(error: {e})"


def _file_hash(path: str) -> str:
  try:
    with open(path, "rb") as f:
      return hashlib.sha1(f.read()).hexdigest()[:12]
  except Exception:
    return "(missing)"


def _append_jsonl(name: str, record: dict, max_bytes: int = _PULSE_MAX_BYTES) -> None:
  try:
    os.makedirs(TRIAGE_DIR, exist_ok=True)
    path = os.path.join(TRIAGE_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) >= max_bytes:
      os.replace(path, path + ".1")
    record.setdefault("t", round(time.time(), 2))  # noqa: TID251 (wall clock is correct for log records)
    with open(path, "a") as f:
      f.write(json.dumps(record, separators=(",", ":")) + "\n")
  except Exception:
    pass


async def _code_identity() -> dict:
  """Everything needed to prove whether the code on disk changed (hypothesis A)."""
  ident = {
    "branch": await _sh(f"{_GIT} rev-parse --abbrev-ref HEAD"),
    "commit": await _sh(f"{_GIT} rev-parse --short HEAD"),
    "dirty": await _sh(f"{_GIT} status --porcelain | head -5"),
    "version": await _sh("cat /data/openpilot/FUNNYPILOT_VERSION"),
    "updater_target": await _sh("cat /data/params/d/UpdaterTargetBranch 2>/dev/null || echo '(unset)'"),
    "staged": await _sh("git -c safe.directory='*' -C /data/safe_staging/finalized rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(none)'"),
    "overlay_consistent": os.path.exists("/data/safe_staging/.overlay_consistent"),
    "hashes": {os.path.basename(p): _file_hash(p) for p in _FEEL_FILES},
  }
  try:
    with open("/proc/sys/kernel/random/boot_id") as f:
      ident["boot_id"] = f.read().strip()
    with open("/proc/uptime") as f:
      ident["uptime_s"] = round(float(f.read().split()[0]), 1)
  except Exception:
    pass
  return ident


async def _boot_snapshot() -> None:
  ident = await _code_identity()
  ident["kind"] = "boot"
  _append_jsonl("code_identity.jsonl", ident)


async def _pulse_task() -> None:
  """Every 10 min, log the code identity — if something swaps the code while the
  car sits parked, this pins down WHEN it happened, not just that it happened."""
  last = None
  while True:
    try:
      ident = await _code_identity()
      key = (ident.get("branch"), ident.get("commit"), json.dumps(ident.get("hashes", {}), sort_keys=True),
             ident.get("updater_target"), ident.get("staged"), bool(ident.get("dirty")))
      if key != last:
        # identity changed (or first pulse): always record, flag the change
        ident["kind"] = "pulse-change" if last is not None else "pulse-start"
        _append_jsonl("code_identity.jsonl", ident)
        last = key
      else:
        _append_jsonl("code_identity.jsonl", {"kind": "pulse-ok", "commit": ident.get("commit"),
                                              "uptime_s": ident.get("uptime_s")})
    except Exception:
      pass
    await asyncio.sleep(_PULSE_PERIOD_S)


async def handle_logs_list(request: web.Request) -> web.Response:
  try:
    files = []
    if os.path.isdir(TRIAGE_DIR):
      for name in sorted(os.listdir(TRIAGE_DIR)):
        if _TRIAGE_NAME_RE.match(name):
          p = os.path.join(TRIAGE_DIR, name)
          st = os.stat(p)
          files.append({"name": name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return web.json_response({"dir": TRIAGE_DIR, "files": files})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_logs_get(request: web.Request) -> web.Response:
  try:
    name = request.match_info["name"]
    if not _TRIAGE_NAME_RE.match(name):
      return web.json_response({"error": "bad name"}, status=400)
    path = os.path.join(TRIAGE_DIR, name)
    if not os.path.isfile(path):
      return web.json_response({"error": "not found"}, status=404)
    tail_kb = min(int(request.query.get("tail_kb", "128")), 2048)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
      if size > tail_kb * 1024:
        f.seek(size - tail_kb * 1024)
        f.readline()  # drop the partial first line
      text = f.read().decode(errors="replace")
    return web.json_response({"name": name, "size": size, "truncated": size > tail_kb * 1024, "text": text})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_logs_mark(request: web.Request) -> web.Response:
  """User-aligned ground truth: 'the issue is happening RIGHT NOW'."""
  try:
    note = ""
    try:
      body = await request.json()
      note = str(body.get("note", ""))[:500]
    except Exception:
      pass
    _append_jsonl("marks.jsonl", {"kind": "user-mark", "note": note})
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def _start_triage_background(app: web.Application) -> None:
  """FunnyPilot v3.6.9 — SCHEDULE the boot snapshot; never AWAIT it here.

  **THIS IS WHY THE DASHBOARD WAS UNREACHABLE AFTER CONNECTING TO A NETWORK.**
  aiohttp runs every `on_startup` handler TO COMPLETION BEFORE IT BINDS THE
  PORT. This one awaited `_boot_snapshot()`, which awaits `_code_identity()`,
  which is six `_sh()` subprocess calls — `git rev-parse` twice, `git status
  --porcelain`, two `cat`s and a `git rev-parse` into safe_staging — plus a
  SHA-1 of every file in `_FEEL_FILES`. Each `_sh` carries a 10 s timeout, so
  the worst case is about a minute during which nothing is listening on 8888
  and the browser simply cannot connect.

  Right after joining a network is the worst moment for exactly those calls:
  the updater is active, the filesystem is busy, and `git status` on this tree
  is not fast. v3.6.8 then made a pre-existing fragility tip over by adding a
  module-scope `swaglog` import whose handler lists `/data/log` and rotates
  before the module finishes importing.

  THE RULE THIS LEAVES BEHIND, and it is the same one `hud/` already has one
  layer in: **a process whose job is to be reachable must become reachable
  first.** Diagnostics are what you do once you are serving, not a toll paid
  before you start.
  """
  app["triage_pulse"] = asyncio.create_task(_pulse_task())
  app["triage_boot"] = _bg(_boot_snapshot())


async def handle_version(request: web.Request) -> web.Response:
  """FunnyPilot v3.4.5: the running version, for the web UI title bar.

  Read from disk on every request rather than reported from EXPECTED_VERSION,
  because the whole point is to show what the device is ACTUALLY running. A
  stale checkout still serving this page would otherwise report the version it
  was supposed to be — the exact "the flash looked fine" failure of v3.4.3.
  `expected` goes back alongside so the UI can flag the mismatch.
  """
  running = ""
  try:
    with open(_VERSION_FILE) as f:
      running = f.read().strip()
  except Exception:
    pass
  branch = await _sh("git -C /data/openpilot -c safe.directory='*' rev-parse --abbrev-ref HEAD", timeout=5)
  return web.json_response({"version": running, "expected": EXPECTED_VERSION, "branch": branch})


async def handle_index(request: web.Request) -> web.Response:
  index_path = os.path.join(_STATIC_DIR, "index.html")
  if os.path.exists(index_path):
    with open(index_path) as f:
      content = f.read()
    return web.Response(content_type="text/html", text=content)
  return web.Response(text="FunnyPilot Terminal Server", content_type="text/html")



# ════════════════════════════════════════════════════════════════════════════
# FunnyPilot v3.6.8 — DRIVING IS THE PRIORITY, AND THIS PROCESS RUNS WHILE IT
# HAPPENS.
#
# `terminal_server` is registered `always_run`, so everything below is live on
# a moving car. That makes the dashboard's failure modes a vehicle concern
# rather than a web concern, and there are exactly three that matter:
#
#   * IT MUST NOT TAKE ANYTHING DOWN WITH IT. Every handler runs behind
#     `_never_5xx`, which turns any unhandled exception into a JSON error. A
#     raise that escapes an aiohttp handler is survivable; one that escapes a
#     background task is not, so `_bg` wraps those too.
#   * IT MUST NOT TAKE MORE THAN ONE CORE. All blocking work goes through
#     `_EXECUTOR`, which is a ONE-WORKER pool at the lowest priority the
#     scheduler will give it. The default executor is sized to CPU count, which
#     on this SoC means a single browser tab could put every core on log
#     parsing while the car is deciding when to brake.
#   * IT MUST NOT DO EXPENSIVE WORK WHILE DRIVING AT ALL. `_onroad()` reads the
#     same `IsOnroad` param manager sets, and the two genuinely expensive
#     endpoints — the first-time timeline parse and a multi-gigabyte download —
#     refuse with a 503 and an explanation rather than competing. Serving an
#     already-indexed drive stays allowed, because that is a sendfile and a
#     cached JSON read.
#
# THE REFUSAL IS THE FEATURE. A dashboard that quietly degrades the car to stay
# responsive has the priority backwards.
# ════════════════════════════════════════════════════════════════════════════

def _nice_worker() -> None:
  """Drop this worker's priority. NEVER RAISES, and that is the point: a
  ThreadPoolExecutor whose initializer raises becomes permanently BROKEN, so
  every later `_run` fails. Losing the nice value costs a little scheduler
  priority; losing the pool costs the whole Drives section."""
  try:
    os.nice(10)
  except Exception:
    pass


_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
  max_workers=1, thread_name_prefix="fp-web", initializer=_nice_worker)


async def _run(fn, *args):
  """Blocking work, off the event loop and onto the one niced worker."""
  return await asyncio.get_running_loop().run_in_executor(
    _EXECUTOR, functools.partial(fn, *args))


def _onroad() -> bool:
  """Is the car driving? False on any doubt.

  FAILING TOWARD 'NOT DRIVING' IS THE RIGHT DIRECTION HERE and it is worth
  saying why, because the instinct is the opposite. This flag gates whether the
  dashboard may do expensive work; reading it wrong in the cautious direction
  means the owner cannot download a drive from their driveway because Params is
  unreadable, which is a bug they cannot diagnose. Reading it wrong the other
  way costs one CPU-second on a niced thread. The car's own protection is the
  single worker and the nice level, not this.
  """
  try:
    from openpilot.common.params import Params
    return bool(Params().get_bool("IsOnroad"))
  except Exception:
    return False


_BUSY_MSG = "The car is driving. This waits until you are parked — driving comes first."


@web.middleware
async def _never_5xx(request: web.Request, handler):
  """Nothing this server does may become an unhandled exception.

  aiohttp would return a 500 and log a traceback, which is fine on a laptop.
  Here the cost of a surprise is a process manager restarts in a loop while the
  car is moving, so every failure is turned into an answer.
  """
  try:
    return await handler(request)
  except web.HTTPException:
    raise
  except asyncio.CancelledError:
    raise
  except Exception as e:
    _log().exception("nav_webserver handler failed: %s", request.rel_url)
    return web.json_response({"error": type(e).__name__, "detail": str(e)[:400]}, status=500)


def _bg(coro):
  """Fire-and-forget, with the exception actually going somewhere.

  A bare `create_task` whose coroutine raises produces a warning nobody sees on
  a device with no console. This logs it instead.
  """
  async def wrapper():
    try:
      await coro
    except Exception:
      _log().exception("nav_webserver background task failed")
  return asyncio.ensure_future(wrapper())


# ════════════════════════════════════════════════════════════════════════════
# FunnyPilot v3.6.8 — DRIVES: catalogue, playback, download, delete.
#
# THE MEMORY RULES ARE IN drive_index.py AND THESE HANDLERS EXIST TO NOT BREAK
# THEM. Three things matter here and each one is a way this could have gone
# wrong on a device that is driving a car:
#
#   * VIDEO AND LOG FILES GO OUT VIA `web.FileResponse`. aiohttp hands those to
#     the kernel (sendfile), so a 2 GB drive download never becomes 2 GB of
#     Python heap. Reading a file to build a Response is the obvious way to
#     write this and it is the one that OOMs.
#   * THE TIMELINE PARSE IS BOUNDED PER REQUEST (`TIMELINE_SEGMENTS_PER_CALL`)
#     and runs in a THREAD, so the event loop keeps serving while a first visit
#     to a long drive works through its segments. The response names what is
#     still pending and the page asks again.
#   * NOTHING IS PRECOMPUTED IN THE BACKGROUND. A drive is parsed the first
#     time somebody looks at it and never again. Warming the whole disk on boot
#     would be the same mistake v3.6.1 found in the autoupdater: work nobody
#     asked for, competing with the car.
# ════════════════════════════════════════════════════════════════════════════

TIMELINE_SEGMENTS_PER_CALL = 4
_DOWNLOAD_CHUNK = 256 * 1024


def _route_arg(request: web.Request) -> str:
  """The route name from the path, whitelisted here as well as in drive_index.

  Two layers on purpose: this one keeps a malformed name from reaching any
  filesystem call at all, and `_safe_segment_dir` is what actually decides
  whether a resolved path is inside the root. Neither is redundant — the first
  is a cheap reject, the second is the one that survives a symlink.
  """
  route = request.match_info.get("route", "")
  if not route or not re.match(r"^[A-Za-z0-9|_-]{1,128}$", route):
    raise web.HTTPBadRequest(text="bad route name")
  return route


async def handle_drives(request: web.Request) -> web.Response:
  """The catalogue. A directory scan, off the event loop."""
  routes = await _run(drive_index.scan_routes)
  stats = await _run(drive_index.storage_stats)
  # `status` is only interesting when the list is empty, and that is precisely
  # when it is worth everything: an empty catalogue has four causes and they
  # look identical on screen. See drive_index.realdata_status.
  status = await _run(drive_index.realdata_status) if not routes else None
  return web.json_response({"drives": routes, "storage": stats, "status": status})


async def handle_storage(request: web.Request) -> web.Response:
  stats = await _run(drive_index.storage_stats)
  return web.json_response(stats)


async def handle_drive_detail(request: web.Request) -> web.Response:
  route = _route_arg(request)
  segs = drive_index.route_segments(route)
  if not segs:
    raise web.HTTPNotFound(text="no such drive")
  cams: dict[str, dict] = {}
  for s in segs:
    d = drive_index.segment_path(route, s)
    if d is None:
      continue
    for fname, (key, label, playable) in drive_index.VIDEO_FILES.items():
      if os.path.exists(os.path.join(d, fname)):
        cams.setdefault(key, {"key": key, "label": label, "file": fname,
                              "playable": playable, "segments": []})["segments"].append(s)
  return web.json_response({
    "route": route, "segments": segs,
    "cameras": sorted(cams.values(), key=lambda c: not c["playable"]),
    "started_at": drive_index.route_started_at(route),
  })


async def handle_drive_timeline(request: web.Request) -> web.Response:
  route = _route_arg(request)
  # Already-indexed segments are a cached JSON read and always allowed; the
  # PARSE budget drops to zero while driving, so an unindexed drive comes back
  # with everything pending instead of putting the CPU on log decompression.
  limit = 0 if _onroad() else TIMELINE_SEGMENTS_PER_CALL
  data = await _run(functools.partial(drive_index.route_timeline, route, limit=limit))
  if not data["segments"]:
    raise web.HTTPNotFound(text="no such drive")
  return web.json_response(data)


async def handle_drive_playlist(request: web.Request) -> web.Response:
  """An HLS playlist over the segments that actually have a qcamera.ts.

  A segment whose video is missing is SKIPPED rather than represented by a gap,
  because a playlist entry pointing at a 404 stalls the player rather than
  advancing past it. The timeline is the thing that keeps honest time.
  """
  route = _route_arg(request)
  segs = [s for s in drive_index.route_segments(route)
          if (d := drive_index.segment_path(route, s))
          and os.path.exists(os.path.join(d, "qcamera.ts"))]
  if not segs:
    raise web.HTTPNotFound(text="no playable video for this drive")
  body = drive_index.hls_playlist(route, segs)
  return web.Response(text=body, content_type="application/vnd.apple.mpegurl",
                      headers={"Cache-Control": "no-cache"})


async def handle_drive_file(request: web.Request) -> web.FileResponse:
  """One file out of one segment. `FileResponse` = sendfile + range support.

  The allow-list is the video and log names loggerd writes and nothing else, so
  this cannot be turned into a general file server for /data by asking for a
  different name.
  """
  route = _route_arg(request)
  try:
    seg = int(request.match_info["seg"])
  except (KeyError, ValueError):
    raise web.HTTPBadRequest(text="bad segment") from None
  name = request.match_info.get("name", "")
  if name not in drive_index.VIDEO_FILES and name not in drive_index.LOG_FILES:
    raise web.HTTPForbidden(text="not a servable file")
  d = drive_index.segment_path(route, seg)
  if d is None:
    raise web.HTTPBadRequest(text="bad segment")
  path = os.path.join(d, name)
  if not os.path.isfile(path):
    raise web.HTTPNotFound(text="no such file")
  ct = "video/mp2t" if name.endswith(".ts") else "application/octet-stream"
  return web.FileResponse(path, headers={"Content-Type": ct})


async def handle_drive_download(request: web.Request) -> web.StreamResponse:
  """A drive as one streamed tar. `what` = video | data | all.

  STREAMED WITH A FIXED BUFFER AND NO COMPRESSION, and both of those are the
  point. The payload is already-compressed video and zstd logs, so gzip would
  spend the CPU of a whole drive to save nothing; and tar is written header-by-
  header straight to the socket, so peak memory is one 256 kB chunk however
  many gigabytes go out.
  """
  route = _route_arg(request)
  if _onroad():
    raise web.HTTPServiceUnavailable(text=_BUSY_MSG)
  what = request.query.get("what", "all")
  if what not in ("video", "data", "all"):
    raise web.HTTPBadRequest(text="what must be video, data or all")
  segs = drive_index.route_segments(route)
  if not segs:
    raise web.HTTPNotFound(text="no such drive")

  wanted: list[str] = []
  if what in ("video", "all"):
    wanted += list(drive_index.VIDEO_FILES)
  if what in ("data", "all"):
    wanted += list(drive_index.LOG_FILES)

  resp = web.StreamResponse(headers={
    "Content-Type": "application/x-tar",
    "Content-Disposition": f'attachment; filename="{route.replace("|", "_")}-{what}.tar"',
  })
  await resp.prepare(request)

  def _hdr(name: str, size: int) -> bytes:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = int(time.time())  # noqa: TID251 - tar headers are wall clock
    return info.tobuf()

  for s in segs:
    d = drive_index.segment_path(route, s)
    if d is None:
      continue
    for fname in wanted:
      path = os.path.join(d, fname)
      if not os.path.isfile(path):
        continue
      size = os.path.getsize(path)
      await resp.write(_hdr(f"{route}--{s}/{fname}", size))
      with open(path, "rb") as fh:
        while chunk := fh.read(_DOWNLOAD_CHUNK):
          await resp.write(chunk)
      pad = (-size) % 512
      if pad:
        await resp.write(b"\0" * pad)
  await resp.write(b"\0" * 1024)   # tar end-of-archive
  await resp.write_eof()
  return resp


async def handle_drive_delete(request: web.Request) -> web.Response:
  route = _route_arg(request)
  removed, freed = await _run(drive_index.delete_route, route)
  if not removed:
    raise web.HTTPNotFound(text="no such drive")
  return web.json_response({"ok": True, "segments": removed, "bytes": freed})


# ── device actions ──────────────────────────────────────────────────────────
#
# ONE PLACE, ONE ALLOW-LIST, AND NO INTERPOLATION ANYWHERE. Every entry is a
# fixed string; nothing from the request is ever part of a command. That is the
# lesson of the v3.5.8 finding, where `branch` reached a root shell because it
# was only checked with `startswith`.
_DEVICE_ACTIONS = {
  "reboot": ("Rebooting", "sudo reboot"),
  "restart": ("Restarting openpilot", "sudo systemctl restart comma"),
  "maps": ("Queued a map refresh", None),
}


async def handle_device_action(request: web.Request) -> web.Response:
  action = request.match_info.get("action", "")
  if action not in _DEVICE_ACTIONS:
    raise web.HTTPBadRequest(text="unknown action")
  label, cmd = _DEVICE_ACTIONS[action]

  if action == "maps":
    # Through Params, exactly as the settings screen does it, rather than by
    # shelling out to mapd. v3.6.1's post-mortem is the reason this is a single
    # explicit request: `OsmDbUpdatesCheck` DELETES the existing database
    # before downloading a replacement, so it must be a thing the owner asks
    # for once and not something a page poll can trigger repeatedly.
    try:
      from openpilot.common.params import Params
      Params().put_bool("OsmDbUpdatesCheck", True)
    except Exception as e:
      raise web.HTTPInternalServerError(text=f"could not queue map update: {e}") from None
    return web.json_response({"ok": True, "message": label})

  _bg(_sh(cmd, timeout=30))
  return web.json_response({"ok": True, "message": label})


def main():
  app = web.Application(middlewares=[_never_5xx])
  app.router.add_get("/", handle_index)
  app.router.add_get("/ws", handle_ws)
  app.router.add_get("/api/version", handle_version)
  app.router.add_get("/api/branches", handle_branches)
  app.router.add_post("/api/flash", handle_flash)
  app.router.add_post("/api/diagnostics", handle_diagnostics)
  app.router.add_get("/api/logs", handle_logs_list)
  app.router.add_get("/api/logs/{name}", handle_logs_get)
  app.router.add_post("/api/logs/mark", handle_logs_mark)
  # v3.6.8 — drives + device actions
  app.router.add_get("/api/storage", handle_storage)
  app.router.add_get("/api/drives", handle_drives)
  app.router.add_get("/api/drives/{route}", handle_drive_detail)
  app.router.add_delete("/api/drives/{route}", handle_drive_delete)
  app.router.add_get("/api/drives/{route}/timeline", handle_drive_timeline)
  app.router.add_get("/api/drives/{route}/hls.m3u8", handle_drive_playlist)
  app.router.add_get("/api/drives/{route}/download", handle_drive_download)
  app.router.add_get("/api/drives/{route}/seg/{seg}/{name}", handle_drive_file)
  app.router.add_post("/api/device/{action}", handle_device_action)
  app.on_startup.append(_start_triage_background)

  if os.path.isdir(_STATIC_DIR):
    app.router.add_static("/static", _STATIC_DIR)

  # ANY failure here exits the process rather than leaving a half-started
  # server holding a port. `terminal_server` is `always_run`, so manager brings
  # it back — a clean exit and a restart is a recoverable state, and a wedged
  # process holding the CPU next to a moving car is not.
  try:
    web.run_app(app, host="0.0.0.0", port=_PORT, reuse_address=True,
                handle_signals=True, print=None)
  except Exception:
    _log().exception("nav_webserver exiting")
    raise


if __name__ == "__main__":
  main()
