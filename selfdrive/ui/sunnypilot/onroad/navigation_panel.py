"""
FunnyPilot NavigationPanel — turn-by-turn onroad overlay.

Bottom-left position, shown only when navigation is active.
"""
import math
import pyray as rl

from openpilot.common.constants import CV
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

METER_TO_MILE = 0.000621371
METER_TO_FOOT = 3.28084

PANEL_W = 440
PANEL_H = 100
PANEL_MARGIN_X = 20
PANEL_MARGIN_Y = 100  # above road name area

BG_COLOR = rl.Color(0, 0, 0, 200)
ARROW_COLOR = rl.WHITE
TEXT_COLOR = rl.WHITE
MUTED_COLOR = rl.Color(180, 180, 180, 255)
ACCENT_COLOR = rl.Color(233, 69, 96, 255)  # FunnyPilot red


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

    self.font_bold = gui_app.font(FontWeight.BOLD)
    self.font_norm = gui_app.font(FontWeight.NORMAL)

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
    except Exception:
      pass

  def _render(self, rect: rl.Rectangle) -> None:
    if not self.nav_active:
      return

    # Bottom-left position
    panel_x = rect.x + PANEL_MARGIN_X
    panel_y = rect.y + rect.height - PANEL_H - PANEL_MARGIN_Y
    panel_rect = rl.Rectangle(panel_x, panel_y, PANEL_W, PANEL_H)

    # Background
    rl.draw_rectangle_rounded(panel_rect, 0.15, 10, BG_COLOR)

    # Arrow area (left side, 80px wide)
    arrow_cx = panel_x + 44
    arrow_cy = panel_y + PANEL_H / 2
    _draw_turn_arrow(arrow_cx, arrow_cy, 52, self.maneuver_modifier)

    # Vertical divider
    rl.draw_line_ex(
      rl.Vector2(panel_x + 88, panel_y + 12),
      rl.Vector2(panel_x + 88, panel_y + PANEL_H - 12),
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
      rl.draw_text_ex(self.font_bold, street_disp,
                      rl.Vector2(text_x + dist_size.x + sep_w, text_y_top + 2), 28, 0, TEXT_COLOR)

    # Row 2: ETA and remaining distance
    text_y_bot = panel_y + 56
    eta_str = _format_time(self.time_remaining)
    remain_str = _format_distance(self.distance_remaining, ui_state.is_metric) + " left"

    rl.draw_text_ex(self.font_norm, eta_str, rl.Vector2(text_x, text_y_bot), 26, 0, MUTED_COLOR)

    remain_w = measure_text_cached(self.font_norm, remain_str, 26).x
    rl.draw_text_ex(self.font_norm, remain_str,
                    rl.Vector2(panel_x + PANEL_W - remain_w - 12, text_y_bot), 26, 0, MUTED_COLOR)
