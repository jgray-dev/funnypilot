"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

FunnyPilot v3.5.0 — onroad HUD, composed from one design system.

LAYOUT: HORIZON BANDS. The camera image has three natural strata — sky, the
road and traffic through the middle, the hood at the bottom. Chrome lives in
the top and bottom bands and the middle third is never drawn into. Stations
inside the bands:

    top-left     set speed, then the speed-limit sign column
    top-centre   road name, the hero speed, the status pill strip
    top-right    the SCC-M route minimap
    left edge    acceleration spine, longitudinal state dot
    bottom       the diagnostics rail (dev UI), alerts

EVERY STATION RESERVES ITS SPACE whether or not it currently has content, so
nothing on this screen can move because something else appeared. That is the
invariant; see hud/stations.py for the reflow bug it was written against.

THE WHEEL BUTTON IS GONE (v3.5.0). Tapping it toggled `ExperimentalMode`
mid-drive; the mode now comes solely from the offroad setting, which is the one
place it can be changed deliberately. Nothing about how the car BEHAVES
changed — selfdrived has always read the param and published
`selfdriveState.experimentalMode`, and that is still what the planner acts on.
The screen only lost a control, so an E2E pill in the status strip keeps the
mode visible.

SAFETY. This process draws the offroad screen too, so a raise here is a UI
boot-loop and a device you cannot flash from. Every new widget goes through
`tokens.safe_draw`, which disables a widget permanently on its first exception
instead of taking the frame down. The pre-existing widgets kept below (alerts,
driver monitoring, turn signals, torque bar, dev UI) are called exactly as they
were, because they are proven and this is not the release to churn them.
"""
import math
import time

import pyray as rl

from openpilot.common.constants import CV
from openpilot.selfdrive.ui.mici.onroad.torque_bar import TorqueBar
from openpilot.selfdrive.ui.sunnypilot.onroad import developer_ui as dev_ui
from openpilot.selfdrive.ui.sunnypilot.onroad.developer_ui import DeveloperUiRenderer
from openpilot.selfdrive.ui.sunnypilot.onroad.road_name import RoadNameRenderer
from openpilot.selfdrive.ui.sunnypilot.onroad.speed_limit import SpeedLimitRenderer
from openpilot.selfdrive.ui.sunnypilot.onroad.turn_signal import TurnSignalController
from openpilot.selfdrive.ui.sunnypilot.onroad.circular_alerts import CircularAlertsRenderer
from openpilot.selfdrive.ui.sunnypilot.onroad.long_status_dot import classify as classify_long
from openpilot.selfdrive.ui.sunnypilot.onroad.hud import tokens as T
from openpilot.selfdrive.ui.sunnypilot.onroad.hud import chrome
from openpilot.selfdrive.ui.sunnypilot.onroad.hud import side_signals, stations
from openpilot.selfdrive.ui.sunnypilot.onroad.hud.speed_sign import SpeedSign
from openpilot.selfdrive.ui.sunnypilot.onroad.hud.route_map import RouteMap
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import read_corner_warning_shm
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.selfdrive.ui.onroad.hud_renderer import HudRenderer
from openpilot.sunnypilot.selfdrive.car.brake_light_shm import read_brake_light
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.sla_shm import read_sla_shm
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import read_learn_shm
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr

SpeedLimitAssistState = None  # resolved lazily; see _sla_states()

# station geometry, in content-rect pixels (device is a fixed 2160x1080)
X_GUTTER = 46
Y_TOP = 40
SET_X = X_GUTTER
SIGN_X = X_GUTTER + stations.SET_W + 26
# v3.5.2: the minimap is a FULL-HEIGHT STRIP down the right side. It has no
# container of its own any more (see hud/route_map.py), so height costs nothing
# but visibility — and at 400 m of range it now shows exactly SCC-M's horizon.
MAP_W = 240
# The minimap sits to the LEFT of the dev-UI right column's slot, not above it.
# That slot is reserved whether or not the dev UI is on, so toggling the dev UI
# never moves the map. Keyed off the dev-UI constants rather than hard-coded so
# the two cannot drift apart: an overlap is invisible in review and obvious on
# the road, and stacking them vertically does not fit (see the note on
# RIGHT_TOP_OFFSET in developer_ui/__init__.py).
MAP_RIGHT_INSET = dev_ui.RIGHT_COL_WIDTH + dev_ui.RIGHT_COL_MARGIN + 24
SPEED_Y = 44
ROADNAME_Y = 6
# v3.5.9: the stack sits just under the set-speed plate, in the column that
# was previously empty. SET_H plus a gap, so the two read as one group.
STACK_Y = Y_TOP + stations.SET_H + 22
SPINE_X = 14
DOT_MARGIN = 20   # v3.5.1: the long-state dot's clearance from both edges

_STATE_COLORS = {
  UIStatus.ENGAGED: T.ENGAGED,
  UIStatus.DISENGAGED: T.DISENGAGED,
  UIStatus.OVERRIDE: T.OVERRIDE,
  UIStatus.LAT_ONLY: T.LAT_ONLY,
  UIStatus.LONG_ONLY: T.LONG_ONLY,
}


_SLA_MODE = None


def _sla_mode():
  """v3.5.6: memoised. This was a bare `from ... import Mode` inside
  `_draw_sign`, i.e. a sys.modules lookup and an attribute walk on every frame
  for a value that never changes. Same lazy-import discipline as _sla_states
  below, just resolved once."""
  global _SLA_MODE
  if _SLA_MODE is None:
    from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Mode
    _SLA_MODE = Mode
  return _SLA_MODE


def _sla_states():
  """The SLA state enum, resolved on first use.

  Deliberately NOT a module-level `custom.LongitudinalPlanSP...` constant: the
  v3.4.2 outage was a capnp module object reaching an eagerly-evaluated
  position, and keeping every capnp lookup inside a function body is the
  cheapest way to guarantee this file can always be imported.
  """
  global SpeedLimitAssistState
  if SpeedLimitAssistState is None:
    from cereal import custom
    SpeedLimitAssistState = custom.LongitudinalPlanSP.SpeedLimit.AssistState
  return SpeedLimitAssistState


class HudRendererSP(HudRenderer):
  def __init__(self):
    super().__init__()
    self.developer_ui = DeveloperUiRenderer()
    self.road_name_renderer = RoadNameRenderer()
    # kept purely as the speed-limit DATA parser; its own renderer is replaced
    # by hud/speed_sign.py and is never called.
    self.speed_limit_renderer = SpeedLimitRenderer()
    self.turn_signal_controller = TurnSignalController()
    self.circular_alerts_renderer = CircularAlertsRenderer()
    # v3.5.1: quieter, fixed-height, and on the HUD's own palette. The height
    # ramp is off (`grow=False`) — the driver already has the feel for the
    # limits, so the bar only needs to be legible, not to grow into the road.
    self._torque_bar = TorqueBar(scale=3.0, always=True, opacity=T.TORQUE_OPACITY,
                                 grow=False, warm_color=T.ATTENTION, hot_color=T.HALT)

    self._sign = SpeedSign()
    self._route_map = RouteMap()
    self._corner_warn = False    # v3.6.6, polled from /dev/shm at 5 Hz
    self._warn_t = 0.0

    self.pcm_cruise_speed: bool = True
    self.show_icbm_status: bool = False
    self.icbm_active_counter: int = 0
    self.speed_cluster: float = 0.0
    self.speed_conv: float = CV.MS_TO_KPH if ui_state.is_metric else CV.MS_TO_MPH

    self._accel: float = 0.0
    # v3.5.2 minimap colour reference — see hud/route_map.expected_speed_at
    self._map_ref_mps: float = 0.0
    self._sla_ratio: float = 0.0
    self._sla_on: bool = False
    self._glow_phase: float = 0.0
    # v3.6.2 — the two side channels. One object per edge, stepped once per
    # frame; see hud/side_signals.py.
    self._sig_left = side_signals.SideSignal()
    self._sig_right = side_signals.SideSignal()
    self._sig_t: float = 0.0
    # v3.5.4 motion + scene adaptation
    self._state_tint = T.EasedColor(T.DISENGAGED)
    self._dot_tint = T.EasedColor(T.NOMINAL)
    self._chrome = T.Eased(1.0)
    self._long_state: str = 'gray'
    self._pills: list = []

  # ── state ───────────────────────────────────────────────────────────────

  def _update_state(self) -> None:
    if ui_state.sm.recv_frame["carState"] < ui_state.started_frame:
      return

    if ui_state.CP_SP is not None:
      self.pcm_cruise_speed = ui_state.CP_SP.pcmCruiseSpeed
    self.speed_conv = CV.MS_TO_KPH if ui_state.is_metric else CV.MS_TO_MPH
    self.speed_cluster = ui_state.sm['carState'].cruiseState.speedCluster * self.speed_conv

    super()._update_state()
    self.road_name_renderer.update()
    self.speed_limit_renderer.update()
    self.turn_signal_controller.update()
    self.circular_alerts_renderer.update()

    self._get_icbm_status()
    T.safe_draw("hud_state", self._update_derived)

  def _update_derived(self) -> None:
    sm = ui_state.sm

    # smoothed longitudinal accel for the spine (same filter the old rocket
    # fuel bar used, so the feel of that readout is unchanged)
    self._accel += (sm['carState'].aEgo - self._accel) / 5.0

    gas_gating = False
    try:
      scc = sm['longitudinalPlanSP'].smartCruiseControl
      gas_gating = bool(scc.vision.gasGating or scc.map.gasGating)
    except Exception:
      pass
    sla_gate = False
    if not gas_gating:
      _, sla_gate = read_sla_shm()

    self._long_state = classify_long(sm['carControl'].longActive,
                                     sm['carOutput'].actuatorsOutput.accel,
                                     gas_gating or sla_gate,
                                     read_brake_light())

    self._pills = self._build_pills(gas_gating, sla_gate)

    # FunnyPilot v3.6.6 — SCC-M v2 has a bend ahead whose learned budget has
    # bottomed out and which still stresses the car. v3.6.5 said so with a
    # banner; the owner asked for the state glow instead, which is the right
    # channel for it — a banner is read, an edge pulse is FELT, and the
    # peripheral edge is already this HUD's engagement-state channel.
    # Polled at 5 Hz: the value changes on the 20 Hz planner and the pulse lasts
    # seconds, so a frame-rate read would be free of information.
    now = time.monotonic()
    if now - self._warn_t > 0.2:
      self._warn_t = now
      try:
        self._corner_warn = read_corner_warning_shm()
      except Exception:
        self._corner_warn = False
    self._update_map_reference()

  def _update_map_reference(self) -> None:
    """What the minimap's colours are measured against. v3.5.2.

    THE SET SPEED, not the posted limit — and the current speed when cruise is
    not set, because then nothing is holding us to anything else. Kept in m/s:
    `self.set_speed` has already been through the base renderer's display-unit
    conversion, and mixing that with mapd's m/s velocities is exactly the kind
    of unit error that looks plausible on screen.
    """
    try:
      cs = ui_state.sm['carState']
      self._map_ref_mps = (float(cs.vCruiseCluster) * CV.KPH_TO_MS
                           if self.is_cruise_set else float(cs.vEgo))
      slr = self.speed_limit_renderer
      states = _sla_states()
      self._sla_on = slr.speed_limit_assist_state in (states.active, states.adapting)
      self._sla_ratio = float(slr.sla_dynamic_offset)
    except Exception:
      self._map_ref_mps, self._sla_ratio, self._sla_on = 0.0, 0.0, False

  def _build_pills(self, scc_gate: bool, sla_gate: bool) -> list:
    """EVERY pill, EVERY frame, in a FIXED order. v3.5.9.

    This used to append CONDITIONALLY, so a source with nothing to say was
    absent and the centred row re-flowed around it -- the one thing the station
    model forbids (see the module docstring in stations.py). The stack is
    positional now: a quiet source is drawn muted in its own row and nothing
    ever moves. `lit` carries all of the information.
    """
    sm = ui_state.sm
    conv = self.speed_conv
    pills: list = []

    for label, attr in (("SCC", "vision"), ("MAP", "map")):
      text, color, lit = label, T.MUTED, False
      try:
        side = getattr(sm['longitudinalPlanSP'].smartCruiseControl, attr)
        if side.enabled and side.active and side.vTarget < 888.0:
          text, color, lit = f"{label} {round(side.vTarget * conv)}", T.LAT_ONLY, True
      except Exception:
        pass
      pills.append(stations.Pill(text, color, lit))

    # SCC-Learn: lit with the cap it is taking, otherwise the number of corners
    # it knows -- an empty store reads as "LRN 0" rather than as a missing
    # feature, which is the difference between "nothing learned yet" and
    # "this is broken".
    text, color, lit = "LRN", T.MUTED, False
    try:
      n, learn_active, _conf = read_learn_shm()
      if learn_active:
        color, lit = T.LAT_ONLY, True
      else:
        text = f"LRN {n}"
    except Exception:
      pass
    pills.append(stations.Pill(text, color, lit))

    gate = bool(scc_gate or sla_gate)
    pills.append(stations.Pill(tr("GAS GATE"), T.ATTENTION if gate else T.MUTED, gate))

    e2e = False
    try:
      e2e = bool(sm['selfdriveState'].experimentalMode)
    except Exception:
      pass
    pills.append(stations.Pill("E2E", T.LONG_ONLY if e2e else T.MUTED, e2e))

    return pills

  def _get_icbm_status(self):
    if not self.pcm_cruise_speed and ui_state.sm['carControl'].enabled:
      if round(self.set_speed) != round(self.speed_cluster):
        self.icbm_active_counter = 3 * gui_app.target_fps
      elif self.icbm_active_counter > 0:
        self.icbm_active_counter -= 1
    else:
      self.icbm_active_counter = 0

    self.show_icbm_status = self.icbm_active_counter > 0

  # ── chrome, called from AugmentedRoadView before the HUD ────────────────

  def state_color(self) -> rl.Color:
    """v3.5.4: cross-faded. Engaging used to CUT from slate to green at the
    frame boundary; on a 120 px glow that is a flash in peripheral vision.

    v3.6.6 — AMBER OVERRIDES THE ENGAGEMENT COLOUR while an unmanageable bend is
    coming up. It goes through the SAME easer, so the change of meaning arrives
    as a cross-fade rather than a flash; and it is `T.ATTENTION`, the fork's one
    amber, so a driver learns one colour for "look at this" rather than one per
    feature."""
    base = _STATE_COLORS.get(ui_state.status, T.DISENGAGED)
    return self._state_tint.update(T.ATTENTION if self._corner_warn else base)

  def chrome_scale(self) -> float:
    """Ambient-adaptive chrome strength, eased so a passing streetlight or a
    tunnel mouth cannot make the vignette pump."""
    try:
      pct = float(ui_state.sm['deviceState'].screenBrightnessPercent)
    except Exception:
      pct = 100.0
    return self._chrome.update(chrome.chrome_scale(pct))

  def glow_intensity(self) -> float:
    """Breathe while the driver is overriding. This is the channel that
    replaces a text banner for 'I am not steering right now'."""
    self._glow_phase = (self._glow_phase + 1.0 / max(gui_app.target_fps, 1)) % 8.0
    # v3.6.6 — the corner warning pulses HARDER AND FASTER than the override
    # breath, and checked first so it wins when both are true. The two must not
    # be confusable: an override breath says "I am not steering", this says
    # "the bend ahead is beyond what I can hold". Deeper swing (0.55..1.15
    # against 0.86..1.10) and roughly twice the rate.
    if self._corner_warn:
      return 0.85 + 0.30 * math.sin(self._glow_phase * math.pi / 0.6)
    if ui_state.status == UIStatus.OVERRIDE:
      return 0.86 + 0.24 * (0.5 + 0.5 * math.sin(self._glow_phase * math.pi / 1.3))
    return 1.0

  # ── the wheel button is gone; nothing in the HUD is tappable now ─────────

  def draw_side_signals(self, rect) -> None:
    """Step both side channels and draw them. Guarded: this is chrome, and the
    process that draws it also draws the offroad screen."""
    try:
      now = time.monotonic()
      dt = (now - self._sig_t) if self._sig_t else 0.0
      self._sig_t = now
      CS = ui_state.sm['carState']
      # THE SIGNALS ARE SHOWN WHETHER OR NOT THE FEATURE TOGGLES ARE ON. These
      # are facts about the car and the road, not openpilot state: the blinker
      # is lit because the driver lit it, and the blind spot is occupied
      # whether or not we are steering.
      self._sig_left.update(dt, CS.leftBlinker, CS.leftBlindspot)
      self._sig_right.update(dt, CS.rightBlinker, CS.rightBlindspot)
      for sig, is_left in ((self._sig_left, True), (self._sig_right, False)):
        if sig.alpha <= 0.0 or sig.kind == side_signals.KIND_NONE:
          continue
        col = T.HALT if sig.kind == side_signals.KIND_BSD else T.ATTENTION
        chrome.draw_side_signal(rect, is_left, col, sig.alpha, sig.centre, sig.extent)
    except Exception:
      pass

  def user_interacting(self) -> bool:
    return False

  # ── render ──────────────────────────────────────────────────────────────

  def _render(self, rect: rl.Rectangle) -> None:
    # NOTE: HudRenderer._render is deliberately NOT called. It draws the old
    # header gradient, the boxed set speed, the centred speed and the wheel
    # button — all replaced below. Its _update_state IS still used.
    T.safe_draw("bands", chrome.draw_bands, rect, self._chrome.x)
    T.safe_draw("speed", self._draw_centre, rect)
    T.safe_draw("set_speed", self._draw_set_speed, rect)
    T.safe_draw("sign", self._draw_sign, rect)
    T.safe_draw("route_map", self._draw_route_map, rect)
    T.safe_draw("strip", stations.draw_status_stack,
                rect.x + SET_X, rect.y + STACK_Y, self._pills)
    T.safe_draw("vitals", self._draw_vitals, rect)

    # ── pre-existing widgets, untouched ───────────────────────────────────
    if ui_state.torque_bar and ui_state.sm['controlsState'].lateralControlState.which() != 'angleState':
      torque_rect = rect
      if ui_state.developer_ui in (DeveloperUiRenderer.DEV_UI_BOTTOM, DeveloperUiRenderer.DEV_UI_BOTH):
        torque_rect = rl.Rectangle(rect.x, rect.y, rect.width, rect.height - DeveloperUiRenderer.BOTTOM_BAR_HEIGHT)
      self._torque_bar.render(torque_rect)

    self.developer_ui.render(rect)
    self.turn_signal_controller.render(rect)
    self.circular_alerts_renderer.render(rect)

  # ── stations ────────────────────────────────────────────────────────────

  def _draw_centre(self, rect: rl.Rectangle) -> None:
    cx = rect.x + rect.width / 2
    stations.draw_road_name(cx, rect.y + ROADNAME_Y,
                            self.road_name_renderer.road_name if ui_state.road_name_toggle else "")
    if not ui_state.hide_v_ego_ui:
      stations.draw_speed(cx, rect.y + SPEED_Y, self.speed)

  def _draw_set_speed(self, rect: rl.Rectangle) -> None:
    if not self.is_cruise_available:
      return
    stations.draw_set_speed(rect.x + SET_X, rect.y + Y_TOP, self.set_speed, self.is_cruise_set,
                            self.speed_cluster if self.show_icbm_status else None)

  def _draw_sign(self, rect: rl.Rectangle) -> None:
    slr = self.speed_limit_renderer
    if ui_state.speed_limit_mode == _sla_mode().off:
      return

    states = _sla_states()
    active = slr.speed_limit_assist_state in (states.active, states.adapting)
    pre = slr.speed_limit_assist_state == states.preActive

    ahead = slr.speed_limit_ahead if slr.speed_limit_ahead_valid else 0.0
    limit = slr.speed_limit_final_last

    # v3.6.0: NO SIGN WHEN THERE IS NOTHING TO PUT ON IT. The face used to draw
    # "--" whenever mapd had no limit -- after a reboot, before a GPS fix, or
    # off the mapped network -- which is a permanent placeholder for a feature
    # that is simply not applicable right now. Nothing else is positioned
    # relative to this station (the pills moved to the left column in v3.5.9),
    # so hiding it cannot reflow anything.
    if not (limit > 0 or ahead > 0 or active or pre):
      return

    overspeed = bool(limit > 0 and round(limit) < round(slr.speed))

    self._sign.render(rect.x + SIGN_X, rect.y + Y_TOP,
                      limit=limit, next_limit=ahead, dist_m=slr.speed_limit_ahead_dist,
                      sla_active=active, pre_active=pre,
                      offset_ratio=slr.sla_dynamic_offset, metric=ui_state.is_metric,
                      overspeed=overspeed, dt=1.0 / max(gui_app.target_fps, 1),
                      # v3.5.9 UNIT FIX: `limit` is speed_limit_final_last,
                      # which SpeedLimitRenderer has ALREADY multiplied by
                      # speed_conv -- it is what gets drawn on the sign face,
                      # so it is mph. v3.5.6 passed a m/s value against it, so
                      # the chevron compared 20.1 to 45 and pointed UP for
                      # every limit above ~20. Both sides are display units.
                      set_speed=self.set_speed if self.is_cruise_set else 0.0)

  def _draw_route_map(self, rect: rl.Rectangle) -> None:
    """ALWAYS DRAWN as of v3.6.6.

    It used to be gated on `smartCruiseControl.map.enabled`, which is SCC-M v2's
    `is_enabled` — `long_enabled and toggle`. `long_enabled` is
    `carControl.enabled`, so with only LATERAL engaged the whole minimap
    vanished, which is exactly when a driver is most interested in what the road
    ahead is doing.
    THE DATA IS THERE EITHER WAY: `_refresh_corners` runs before the
    `is_enabled` check in `update()` precisely so the geometry keeps being
    measured with the feature off, and `write_corners_shm` publishes it every
    frame. The gate was hiding a live widget, not saving any work.
    The slot was always reserved, so nothing shifts."""
    x = rect.x + rect.width - MAP_W - MAP_RIGHT_INSET
    self._route_map.render(rl.Rectangle(x, rect.y, MAP_W, rect.height),
                           self._map_ref_mps, self._sla_ratio, self._sla_on,
                           ui_state.is_metric)

  def _draw_vitals(self, rect: rl.Rectangle) -> None:
    if ui_state.rocket_fuel:
      stations.draw_accel_spine(rect.x + SPINE_X, rect.y + rect.height / 2, self._accel)
    # v3.5.1: bottom-left corner, DOT_MARGIN clear of both edges. It still
    # clears the dev-UI bottom rail when that is on, because the rail owns the
    # bottom of the content area and nothing may sit under it.
    stations.draw_long_dot(rect.x + DOT_MARGIN + 16,
                           rect.y + rect.height - DOT_MARGIN - 16
                           - DeveloperUiRenderer.get_bottom_dev_ui_offset(),
                           self._dot_tint.update(stations.long_dot_color(self._long_state)))
