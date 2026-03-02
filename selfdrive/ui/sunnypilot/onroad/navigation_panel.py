"""FunnyPilot NavigationPanel - minimal AR-style onroad guidance overlay."""

from __future__ import annotations

import time

import pyray as rl
from cereal import log

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

METER_TO_MILE = 0.000621371
METER_TO_FOOT = 3.28084

ACCENT_COLOR = rl.Color(73, 244, 255, 235)
RIBBON_COLOR = rl.Color(20, 194, 219, 46)
RIBBON_LINE = rl.Color(90, 250, 255, 170)
LANE_LINE_COLOR = rl.Color(255, 255, 255, 130)
TEXT_COLOR = rl.WHITE
MUTED_COLOR = rl.Color(196, 206, 216, 220)
STATUS_OK = rl.Color(112, 232, 149, 255)

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection


def _format_distance(meters: float, is_metric: bool) -> str:
  meters = max(0.0, meters)
  if is_metric:
    if meters < 1000:
      return f"{int(round(meters / 10) * 10)} m"
    return f"{meters / 1000:.1f} km"

  feet = meters * METER_TO_FOOT
  if feet < 900:
    return f"{int(round(feet / 50) * 50)} ft"
  return f"{meters * METER_TO_MILE:.1f} mi"


def _format_time(seconds: float) -> str:
  seconds = max(0.0, seconds)
  if seconds < 60:
    return f"{int(seconds)} sec"
  minutes = round(seconds / 60)
  if minutes < 60:
    return f"{minutes} min"
  hours = minutes // 60
  mins = minutes % 60
  return f"{hours}h {mins}m"


def _modifier_shift(modifier: str) -> int:
  m = (modifier or "").lower()
  if "left" in m:
    return -1
  if "right" in m:
    return 1
  return 0


def _modifier_strength(modifier: str) -> float:
  m = (modifier or "").lower().replace("-", "")
  if "uturn" in m:
    return -1.0 if "left" in m else 1.0
  if "slightleft" in m:
    return -0.45
  if "slightright" in m:
    return 0.45
  if "left" in m:
    return -1.0
  if "right" in m:
    return 1.0
  return 0.0


def _lane_change_shift(direction: int) -> int:
  if direction == LaneChangeDirection.left:
    return -1
  if direction == LaneChangeDirection.right:
    return 1
  return 0


def _active_lane_index(lanes: list[dict]) -> int:
  for idx, lane in enumerate(lanes):
    if lane.get("active", False):
      return idx
  return -1


def _read_lanes(nav_instruction) -> list[dict]:
  return [{"active": bool(getattr(l, "active", False))} for l in getattr(nav_instruction, "lanes", [])]


def _status_text(
  lane_change_state: int, lane_change_direction: int, left_blinker: bool, right_blinker: bool, ego_idx: int, target_idx: int
) -> tuple[str, rl.Color]:
  if lane_change_state == LaneChangeState.preLaneChange:
    return ("Lane change armed", ACCENT_COLOR)
  if lane_change_state in (LaneChangeState.laneChangeStarting, LaneChangeState.laneChangeFinishing):
    if lane_change_direction == LaneChangeDirection.left:
      return ("Changing lanes left", ACCENT_COLOR)
    if lane_change_direction == LaneChangeDirection.right:
      return ("Changing lanes right", ACCENT_COLOR)
    return ("Changing lanes", ACCENT_COLOR)
  if left_blinker and not right_blinker:
    return ("Blinker left", ACCENT_COLOR)
  if right_blinker and not left_blinker:
    return ("Blinker right", ACCENT_COLOR)
  if target_idx < ego_idx:
    return ("Prepare left lane", ACCENT_COLOR)
  if target_idx > ego_idx:
    return ("Prepare right lane", ACCENT_COLOR)
  return ("Lane aligned", STATUS_OK)


def _draw_quad(a: rl.Vector2, b: rl.Vector2, c: rl.Vector2, d: rl.Vector2, color: rl.Color) -> None:
  rl.draw_triangle(a, b, c, color)
  rl.draw_triangle(a, c, d, color)


def _truncate(text: str, max_px: float, font, size: int) -> str:
  if measure_text_cached(font, text, size).x <= max_px:
    return text

  trimmed = text
  while trimmed and measure_text_cached(font, trimmed + "...", size).x > max_px:
    trimmed = trimmed[:-1]
  return (trimmed + "...") if trimmed else ""


class NavigationPanel(Widget):
  def __init__(self):
    super().__init__()
    self.nav_active = False
    self.maneuver_text = ""
    self.maneuver_modifier = "straight"
    self.distance_to_maneuver = 0.0
    self.distance_to_maneuver_display = 0.0
    self.distance_remaining = 0.0
    self.distance_remaining_display = 0.0
    self.time_remaining = 0.0
    self.car_speed = 0.0
    self.lane_confidence = 0.5
    self.lanes = []
    self.ego_lane_idx = 1
    self.target_lane_idx = 1
    self.lane_change_state = LaneChangeState.off
    self.prev_lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.prev_lane_change_direction = LaneChangeDirection.none
    self.left_blinker = False
    self.right_blinker = False

    self.last_nav_update = 0.0
    self.last_frame_time = time.monotonic()

    self.font_bold = gui_app.font()
    self.font_norm = gui_app.font()

  def _update_lane_model(self) -> None:
    lane_count = max(3, len(self.lanes))
    self.ego_lane_idx = max(0, min(self.ego_lane_idx, lane_count - 1))

    active_idx = _active_lane_index(self.lanes)
    if active_idx >= 0:
      self.target_lane_idx = active_idx
    else:
      shift = _modifier_shift(self.maneuver_modifier)
      self.target_lane_idx = max(0, min(self.ego_lane_idx + shift, lane_count - 1))

    if self.lane_change_state in (LaneChangeState.preLaneChange, LaneChangeState.laneChangeStarting, LaneChangeState.laneChangeFinishing):
      lane_change_shift = _lane_change_shift(self.lane_change_direction)
      if lane_change_shift != 0:
        self.target_lane_idx = max(0, min(self.ego_lane_idx + lane_change_shift, lane_count - 1))

    transition_done = self.prev_lane_change_state == LaneChangeState.laneChangeFinishing and self.lane_change_state == LaneChangeState.off
    if transition_done:
      final_shift = _lane_change_shift(self.prev_lane_change_direction)
      if final_shift != 0:
        self.ego_lane_idx = max(0, min(self.ego_lane_idx + final_shift, lane_count - 1))

    self.prev_lane_change_state = self.lane_change_state
    self.prev_lane_change_direction = self.lane_change_direction

  def _smooth_distances(self, dt: float) -> None:
    if not self.nav_active:
      self.distance_to_maneuver_display = 0.0
      self.distance_remaining_display = 0.0
      return

    seconds_since_nav = time.monotonic() - self.last_nav_update
    if seconds_since_nav > 3.0:
      return

    decay = max(0.0, self.car_speed) * dt
    self.distance_to_maneuver_display = max(0.0, self.distance_to_maneuver_display - decay)
    self.distance_remaining_display = max(0.0, self.distance_remaining_display - decay)

  def update(self):
    now = time.monotonic()
    dt = max(0.0, min(0.25, now - self.last_frame_time))
    self.last_frame_time = now

    sm = ui_state.sm
    try:
      if sm.updated.get("carState", False):
        cs = sm["carState"]
        self.left_blinker = bool(cs.leftBlinker)
        self.right_blinker = bool(cs.rightBlinker)
        self.car_speed = float(cs.vEgo)

      if sm.updated.get("navigationStateSP", False):
        ns = sm["navigationStateSP"]
        self.nav_active = bool(ns.active)
        self.distance_remaining = max(0.0, float(ns.distanceRemaining))
        self.distance_remaining_display = self.distance_remaining
        self.time_remaining = max(0.0, float(ns.timeRemaining))
        self.last_nav_update = now

      if sm.updated.get("navInstruction", False):
        ni = sm["navInstruction"]
        self.maneuver_text = ni.maneuverPrimaryText
        self.maneuver_modifier = ni.maneuverModifier or "straight"
        self.distance_to_maneuver = max(0.0, float(ni.maneuverDistance))
        self.distance_to_maneuver_display = self.distance_to_maneuver
        self.lanes = _read_lanes(ni)
        self.last_nav_update = now

      if sm.updated.get("modelV2", False):
        model = sm["modelV2"]
        meta = model.meta
        self.lane_change_state = meta.laneChangeState
        self.lane_change_direction = meta.laneChangeDirection

        lane_probs = [float(p) for p in getattr(model, "laneLineProbs", [])[:4]]
        if lane_probs:
          self.lane_confidence = max(0.15, min(1.0, sum(lane_probs) / len(lane_probs)))

      self._update_lane_model()
      self._smooth_distances(dt)
    except Exception:
      pass

  def _draw_ribbon(self, rect: rl.Rectangle) -> tuple[float, float, float, float, float, int]:
    lane_count = max(3, min(6, len(self.lanes) if self.lanes else 3))
    center_x = rect.x + rect.width * 0.5
    horizon_y = rect.y + rect.height * 0.50
    base_y = rect.y + rect.height - 102

    half_near = 154 + lane_count * 16
    half_far = 46 + lane_count * 7

    lane_span = max(1.0, (lane_count - 1) * 0.5)
    lane_center = (self.target_lane_idx - lane_span) / lane_span
    lane_center = max(-1.0, min(1.0, lane_center))

    turn_strength = _modifier_strength(self.maneuver_modifier)
    shift_px = lane_center * 120 + turn_strength * 30

    p1 = rl.Vector2(center_x - half_near + shift_px * 0.22, base_y)
    p2 = rl.Vector2(center_x + half_near + shift_px * 0.22, base_y)
    p3 = rl.Vector2(center_x + half_far + shift_px, horizon_y)
    p4 = rl.Vector2(center_x - half_far + shift_px, horizon_y)

    alpha_scale = 0.35 + 0.65 * self.lane_confidence
    fill = rl.Color(RIBBON_COLOR.r, RIBBON_COLOR.g, RIBBON_COLOR.b, int(RIBBON_COLOR.a * alpha_scale))
    line = rl.Color(RIBBON_LINE.r, RIBBON_LINE.g, RIBBON_LINE.b, int(RIBBON_LINE.a * alpha_scale))

    _draw_quad(p1, p2, p3, p4, fill)
    rl.draw_line_ex(p1, p4, 2.5, line)
    rl.draw_line_ex(p2, p3, 2.5, line)

    for i in range(1, lane_count):
      t = i / lane_count
      near_x = p1.x + (p2.x - p1.x) * t
      far_x = p4.x + (p3.x - p4.x) * t
      rl.draw_line_ex(rl.Vector2(near_x, base_y), rl.Vector2(far_x, horizon_y), 1.6, LANE_LINE_COLOR)

    target_left_t = self.target_lane_idx / lane_count
    target_right_t = (self.target_lane_idx + 1) / lane_count
    tl_near = p1.x + (p2.x - p1.x) * target_left_t
    tr_near = p1.x + (p2.x - p1.x) * target_right_t
    tl_far = p4.x + (p3.x - p4.x) * target_left_t
    tr_far = p4.x + (p3.x - p4.x) * target_right_t
    _draw_quad(
      rl.Vector2(tl_near, base_y),
      rl.Vector2(tr_near, base_y),
      rl.Vector2(tr_far, horizon_y),
      rl.Vector2(tl_far, horizon_y),
      rl.Color(73, 244, 255, int(52 * alpha_scale)),
    )

    car_x = center_x
    car_y = base_y - 12
    rl.draw_triangle(
      rl.Vector2(car_x, car_y - 15),
      rl.Vector2(car_x - 9, car_y + 2),
      rl.Vector2(car_x + 9, car_y + 2),
      rl.Color(240, 246, 252, 240),
    )

    return center_x, horizon_y, base_y, shift_px, turn_strength, lane_count

  def _draw_turn_cue(self, center_x: float, horizon_y: float, shift_px: float, turn_strength: float) -> None:
    tip_x = center_x + shift_px * 0.38 + turn_strength * 72
    tip_y = horizon_y - 30
    width = 26
    height = 22
    rl.draw_triangle(
      rl.Vector2(tip_x, tip_y),
      rl.Vector2(tip_x - width, tip_y + height),
      rl.Vector2(tip_x + width, tip_y + height),
      ACCENT_COLOR,
    )
    tail_x = tip_x - turn_strength * 34
    rl.draw_line_ex(rl.Vector2(tail_x, tip_y + height + 6), rl.Vector2(tip_x, tip_y + height + 1), 3.0, ACCENT_COLOR)

  def _render(self, rect: rl.Rectangle) -> None:
    if not self.nav_active:
      return

    center_x, horizon_y, base_y, shift_px, turn_strength, _ = self._draw_ribbon(rect)
    self._draw_turn_cue(center_x, horizon_y, shift_px, turn_strength)

    dist_str = _format_distance(self.distance_to_maneuver_display, ui_state.is_metric)
    dist_size = measure_text_cached(self.font_bold, dist_str, 66)
    rl.draw_text_ex(self.font_bold, dist_str, rl.Vector2(center_x - dist_size.x * 0.5, horizon_y + 18), 66, 0, TEXT_COLOR)

    maneuver = _truncate(self.maneuver_text or "Continue", 500, self.font_bold, 30)
    m_size = measure_text_cached(self.font_bold, maneuver, 30)
    rl.draw_text_ex(self.font_bold, maneuver, rl.Vector2(center_x - m_size.x * 0.5, horizon_y + 88), 30, 0, MUTED_COLOR)

    remain_str = f"{_format_distance(self.distance_remaining_display, ui_state.is_metric)} left"
    eta_str = _format_time(self.time_remaining)
    meta = f"{eta_str}  |  {remain_str}"
    meta_size = measure_text_cached(self.font_norm, meta, 24)
    meta_x = rect.x + rect.width - meta_size.x - 28
    meta_y = rect.y + 64
    rl.draw_text_ex(self.font_norm, meta, rl.Vector2(meta_x, meta_y), 24, 0, MUTED_COLOR)

    status_text, status_color = _status_text(
      self.lane_change_state,
      self.lane_change_direction,
      self.left_blinker,
      self.right_blinker,
      self.ego_lane_idx,
      self.target_lane_idx,
    )
    status_size = measure_text_cached(self.font_norm, status_text, 22)
    rl.draw_text_ex(self.font_norm, status_text, rl.Vector2(center_x - status_size.x * 0.5, base_y + 10), 22, 0, status_color)
