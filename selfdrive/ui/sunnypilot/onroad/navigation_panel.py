"""
FunnyPilot NavigationPanel — turn-by-turn onroad overlay.

Bottom-left position, shown only when navigation is active.
"""

from __future__ import annotations

import math
import pyray as rl
from cereal import log

from openpilot.common.constants import CV
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

METER_TO_MILE = 0.000621371
METER_TO_FOOT = 3.28084

PANEL_W = 440
PANEL_H = 100
PANEL_H_WITH_LANES = 184
PANEL_MARGIN_X = 20
PANEL_MARGIN_Y = 100  # above road name area

BG_COLOR = rl.Color(0, 0, 0, 200)
ARROW_COLOR = rl.WHITE
TEXT_COLOR = rl.WHITE
MUTED_COLOR = rl.Color(180, 180, 180, 255)
ACCENT_COLOR = rl.Color(233, 69, 96, 255)  # FunnyPilot red
LANE_ACTIVE_BG = rl.Color(233, 69, 96, 210)
LANE_BG = rl.Color(255, 255, 255, 20)
GUIDE_BG = rl.Color(255, 255, 255, 12)
GUIDE_TARGET = rl.Color(73, 244, 255, 120)
GUIDE_CAR = rl.Color(255, 255, 255, 235)
STATUS_OK = rl.Color(112, 232, 149, 255)

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection


def _draw_turn_arrow(cx: float, cy: float, size: float, modifier: str) -> None:
  """Draw a directional arrow using raylib primitives based on maneuver modifier."""
  s = size
  half = s * 0.5
  shaft_w = s * 0.22
  head_w = s * 0.5
  head_h = s * 0.4

  modifier = (modifier or "straight").lower()

  if "u-turn" in modifier or "uturn" in modifier:
    # U-turn: arc + downward arrow
    rl.draw_ring(rl.Vector2(cx, cy - s * 0.1), s * 0.25, s * 0.4, 0, 180, 20, ARROW_COLOR)
    # Downward arrowhead on right side
    rl.draw_triangle(
      rl.Vector2(cx + s * 0.4 - head_w / 2, cy - s * 0.1),
      rl.Vector2(cx + s * 0.4 + head_w / 2, cy - s * 0.1),
      rl.Vector2(cx + s * 0.4, cy - s * 0.1 + head_h),
      ARROW_COLOR,
    )
    return

  if "slight-left" in modifier or "slight left" in modifier or "slightleft" in modifier:
    _draw_angled_arrow(cx, cy, size, -30)
    return

  if "slight-right" in modifier or "slight right" in modifier or "slightright" in modifier:
    _draw_angled_arrow(cx, cy, size, 30)
    return

  if "left" in modifier:
    _draw_angled_arrow(cx, cy, size, -90)
    return

  if "right" in modifier:
    _draw_angled_arrow(cx, cy, size, 90)
    return

  # straight / default: vertical up arrow
  # shaft
  shaft_rect = rl.Rectangle(cx - shaft_w / 2, cy + half * 0.1, shaft_w, half * 0.7)
  rl.draw_rectangle_rec(shaft_rect, ARROW_COLOR)
  # arrowhead (pointing up)
  rl.draw_triangle(
    rl.Vector2(cx - head_w / 2, cy + half * 0.1),
    rl.Vector2(cx + head_w / 2, cy + half * 0.1),
    rl.Vector2(cx, cy - half * 0.7),
    ARROW_COLOR,
  )


def _draw_angled_arrow(cx: float, cy: float, size: float, angle_deg: float) -> None:
  """Draw a turning arrow — shaft goes up then curves to direction."""
  s = size
  half = s * 0.5

  rad = math.radians(angle_deg)
  # Shaft base (pointing up)
  shaft_w = s * 0.2
  rl.draw_rectangle_rec(rl.Rectangle(cx - shaft_w / 2, cy, shaft_w, half * 0.5), ARROW_COLOR)

  # Direction vector for arrowhead
  dx = math.sin(rad)
  dy = -math.cos(rad)

  # Arrowhead tip
  tip_x = cx + dx * half * 0.7
  tip_y = cy - half * 0.3 + dy * half * 0.5

  perp_x = -dy * half * 0.35
  perp_y = dx * half * 0.35

  rl.draw_triangle(
    rl.Vector2(tip_x - perp_x, tip_y - perp_y),
    rl.Vector2(tip_x + perp_x, tip_y + perp_y),
    rl.Vector2(tip_x + dx * half * 0.45, tip_y + dy * half * 0.45),
    ARROW_COLOR,
  )


def _format_distance(meters: float, is_metric: bool) -> str:
  if is_metric:
    if meters < 1000:
      return f"{int(round(meters / 10) * 10)} m"
    return f"{meters / 1000:.1f} km"
  else:
    feet = meters * METER_TO_FOOT
    if feet < 900:
      return f"{int(round(feet / 50) * 50)} ft"
    return f"{meters * METER_TO_MILE:.1f} mi"


def _format_time(seconds: float) -> str:
  if seconds < 60:
    return f"{int(seconds)} sec"
  minutes = round(seconds / 60)
  if minutes < 60:
    return f"{minutes} min"
  hours = minutes // 60
  mins = minutes % 60
  return f"{hours}h {mins}m"


def _normalize_lane_direction(direction) -> str:
  if direction is None:
    return "none"

  if isinstance(direction, int):
    int_map = {
      0: "none",
      1: "left",
      2: "right",
      3: "straight",
      4: "slightleft",
      5: "slightright",
    }
    return int_map.get(direction, "none")

  text = str(direction).strip().lower().replace("-", "")
  if "slightleft" in text:
    return "slightleft"
  if "slightright" in text:
    return "slightright"
  if "left" in text:
    return "left"
  if "right" in text:
    return "right"
  if "straight" in text:
    return "straight"
  return "none"


def _direction_label(direction: str) -> str:
  if direction == "left":
    return "L"
  if direction == "right":
    return "R"
  if direction == "straight":
    return "S"
  if direction == "slightleft":
    return "SL"
  if direction == "slightright":
    return "SR"
  return "."


def _modifier_shift(modifier: str) -> int:
  m = (modifier or "").lower()
  if "left" in m:
    return -1
  if "right" in m:
    return 1
  return 0


def _lane_change_shift(direction: int) -> int:
  if direction == LaneChangeDirection.left:
    return -1
  if direction == LaneChangeDirection.right:
    return 1
  return 0


def _active_lane_index(lanes: list) -> int:
  for idx, lane in enumerate(lanes):
    if lane.get("active", False):
      return idx
  return -1


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


def _read_lanes(nav_instruction) -> list:
  lanes = []
  for lane in getattr(nav_instruction, "lanes", []):
    directions = [_normalize_lane_direction(d) for d in getattr(lane, "directions", [])]
    directions = [d for d in directions if d != "none"]
    if not directions:
      directions = ["none"]

    active_direction = _normalize_lane_direction(getattr(lane, "activeDirection", "none"))
    if active_direction == "none":
      active_direction = directions[0]

    lanes.append(
      {
        "active": bool(getattr(lane, "active", False)),
        "directions": directions,
        "activeDirection": active_direction,
      }
    )
  return lanes


def _draw_lane_guidance(panel_x: float, panel_y: float, panel_w: float, panel_h: float, font, lanes: list) -> None:
  if not lanes:
    return

  lanes = lanes[:8]
  lane_w = 30
  lane_h = 20
  gap = 5
  total_w = len(lanes) * lane_w + max(0, len(lanes) - 1) * gap
  start_x = panel_x + panel_w - total_w - 12
  y = panel_y + panel_h - lane_h - 10

  label = "Lane"
  rl.draw_text_ex(font, label, rl.Vector2(panel_x + 100, y + 1), 18, 0, MUTED_COLOR)

  for i, lane in enumerate(lanes):
    x = start_x + i * (lane_w + gap)
    lane_rect = rl.Rectangle(x, y, lane_w, lane_h)
    active = lane.get("active", False)

    rl.draw_rectangle_rounded(lane_rect, 0.22, 6, LANE_ACTIVE_BG if active else LANE_BG)
    rl.draw_rectangle_rounded_lines_ex(lane_rect, 0.22, 6, 1.5, rl.Color(255, 255, 255, 70))

    directions = lane.get("directions", ["none"])
    if active:
      text = _direction_label(lane.get("activeDirection", directions[0]))
      text_color = rl.WHITE
    else:
      labels = [_direction_label(d) for d in directions[:2]]
      text = "/".join(labels)
      text_color = MUTED_COLOR

    text_w = measure_text_cached(font, text, 15).x
    rl.draw_text_ex(font, text, rl.Vector2(x + (lane_w - text_w) / 2, y + 2), 15, 0, text_color)


def _draw_lane_overview(panel_x: float, panel_y: float, font, lanes: list, ego_lane_idx: int, target_lane_idx: int) -> None:
  lane_count = max(2, min(len(lanes), 6))
  view_w = 132
  view_h = 62
  view_x = panel_x + 100
  view_y = panel_y + 96

  road_rect = rl.Rectangle(view_x, view_y, view_w, view_h)
  rl.draw_rectangle_rounded(road_rect, 0.16, 8, GUIDE_BG)
  rl.draw_rectangle_rounded_lines_ex(road_rect, 0.16, 8, 1.5, rl.Color(255, 255, 255, 65))

  inner_x = view_x + 10
  inner_y = view_y + 6
  inner_w = view_w - 20
  inner_h = view_h - 12
  lane_w = inner_w / lane_count

  if 0 <= target_lane_idx < lane_count:
    target_rect = rl.Rectangle(inner_x + target_lane_idx * lane_w + 1, inner_y + 1, lane_w - 2, inner_h - 2)
    rl.draw_rectangle_rec(target_rect, GUIDE_TARGET)

  for i in range(1, lane_count):
    x = inner_x + i * lane_w
    rl.draw_line_ex(rl.Vector2(x, inner_y + 3), rl.Vector2(x, inner_y + inner_h - 3), 1.5, rl.Color(255, 255, 255, 95))

  ego_lane_idx = max(0, min(ego_lane_idx, lane_count - 1))
  car_x = inner_x + ego_lane_idx * lane_w + lane_w * 0.5
  car_y = inner_y + inner_h - 8
  rl.draw_triangle(
    rl.Vector2(car_x, car_y - 14),
    rl.Vector2(car_x - 8, car_y),
    rl.Vector2(car_x + 8, car_y),
    GUIDE_CAR,
  )

  legend = "Target lane"
  rl.draw_text_ex(font, legend, rl.Vector2(view_x + view_w + 8, view_y + 20), 16, 0, MUTED_COLOR)


class NavigationPanel(Widget):
  def __init__(self):
    super().__init__()
    self.nav_active = False
    self.maneuver_text = ""
    self.maneuver_type = ""
    self.maneuver_modifier = "straight"
    self.distance_to_maneuver = 0.0
    self.distance_remaining = 0.0
    self.time_remaining = 0.0
    self.lanes = []
    self.ego_lane_idx = 1
    self.target_lane_idx = 1
    self.lane_change_state = LaneChangeState.off
    self.prev_lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.prev_lane_change_direction = LaneChangeDirection.none
    self.left_blinker = False
    self.right_blinker = False

    self.font_bold = gui_app.font(FontWeight.BOLD)
    self.font_norm = gui_app.font(FontWeight.NORMAL)

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

  def update(self):
    sm = ui_state.sm

    try:
      # Read navigationStateSP
      if sm.updated.get("navigationStateSP", False):
        ns = sm["navigationStateSP"]
        self.nav_active = ns.active
        self.distance_remaining = ns.distanceRemaining
        self.time_remaining = ns.timeRemaining

      # Read navInstruction
      if sm.updated.get("navInstruction", False):
        ni = sm["navInstruction"]
        self.maneuver_text = ni.maneuverPrimaryText
        self.maneuver_type = ni.maneuverType
        self.maneuver_modifier = ni.maneuverModifier or "straight"
        self.distance_to_maneuver = ni.maneuverDistance
        self.lanes = _read_lanes(ni)

      if sm.updated.get("modelV2", False):
        meta = sm["modelV2"].meta
        self.lane_change_state = meta.laneChangeState
        self.lane_change_direction = meta.laneChangeDirection

      if sm.updated.get("carState", False):
        cs = sm["carState"]
        self.left_blinker = bool(cs.leftBlinker)
        self.right_blinker = bool(cs.rightBlinker)

      self._update_lane_model()
    except Exception:
      pass

  def _render(self, rect: rl.Rectangle) -> None:
    if not self.nav_active:
      return

    # Bottom-left position
    panel_x = rect.x + PANEL_MARGIN_X
    panel_h = PANEL_H_WITH_LANES
    panel_y = rect.y + rect.height - panel_h - PANEL_MARGIN_Y
    panel_rect = rl.Rectangle(panel_x, panel_y, PANEL_W, panel_h)

    # Background
    rl.draw_rectangle_rounded(panel_rect, 0.15, 10, BG_COLOR)

    # Arrow area (left side, 80px wide)
    arrow_cx = panel_x + 44
    arrow_cy = panel_y + panel_h / 2
    _draw_turn_arrow(arrow_cx, arrow_cy, 52, self.maneuver_modifier)

    # Vertical divider
    rl.draw_line_ex(
      rl.Vector2(panel_x + 88, panel_y + 12),
      rl.Vector2(panel_x + 88, panel_y + panel_h - 12),
      1.5,
      rl.Color(255, 255, 255, 60),
    )

    # Text area (right side)
    text_x = panel_x + 100
    text_y_top = panel_y + 14

    # Row 1: distance + road name
    dist_str = _format_distance(self.distance_to_maneuver, ui_state.is_metric)
    dist_size = measure_text_cached(self.font_bold, dist_str, 32)
    rl.draw_text_ex(self.font_bold, dist_str, rl.Vector2(text_x, text_y_top), 32, 0, ACCENT_COLOR)

    sep = " · "
    sep_w = measure_text_cached(self.font_norm, sep, 28).x
    rl.draw_text_ex(self.font_norm, sep, rl.Vector2(text_x + dist_size.x, text_y_top + 2), 28, 0, MUTED_COLOR)

    street = self.maneuver_text or ""
    if street:
      # Truncate if too long
      max_w = PANEL_W - 110 - dist_size.x - sep_w - 10
      street_disp = street
      while street_disp and measure_text_cached(self.font_bold, street_disp, 28).x > max_w:
        street_disp = street_disp[:-1]
      if street_disp != street:
        street_disp = street_disp[:-1] + "…"
      rl.draw_text_ex(self.font_bold, street_disp, rl.Vector2(text_x + dist_size.x + sep_w, text_y_top + 2), 28, 0, TEXT_COLOR)

    # Row 2: ETA and remaining distance
    text_y_bot = panel_y + 56
    eta_str = _format_time(self.time_remaining)
    remain_str = _format_distance(self.distance_remaining, ui_state.is_metric) + " left"

    rl.draw_text_ex(self.font_norm, eta_str, rl.Vector2(text_x, text_y_bot), 26, 0, MUTED_COLOR)

    remain_w = measure_text_cached(self.font_norm, remain_str, 26).x
    rl.draw_text_ex(self.font_norm, remain_str, rl.Vector2(panel_x + PANEL_W - remain_w - 12, text_y_bot), 26, 0, MUTED_COLOR)

    lanes_for_overview = self.lanes if self.lanes else [{"active": False}] * 3
    _draw_lane_overview(panel_x, panel_y, self.font_norm, lanes_for_overview, self.ego_lane_idx, self.target_lane_idx)

    status_text, status_color = _status_text(
      self.lane_change_state,
      self.lane_change_direction,
      self.left_blinker,
      self.right_blinker,
      self.ego_lane_idx,
      self.target_lane_idx,
    )
    status_w = measure_text_cached(self.font_norm, status_text, 20).x
    status_x = panel_x + PANEL_W - status_w - 14
    status_y = panel_y + 118
    rl.draw_text_ex(self.font_norm, status_text, rl.Vector2(status_x, status_y), 20, 0, status_color)

    if self.lanes:
      _draw_lane_guidance(panel_x, panel_y, PANEL_W, panel_h, self.font_norm, self.lanes)
