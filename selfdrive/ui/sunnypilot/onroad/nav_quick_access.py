"""
FunnyPilot NavQuickAccess — pre-drive Home/Work/Refresh shortcut buttons.

Visible when openpilot started but car is not in Drive gear.
"""

import json
import math
import time
import pyray as rl

from cereal import car
from openpilot.common.params import Params, UnknownKeyName
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

GearShifter = car.CarState.GearShifter

BTN_W = 140
BTN_H = 100
BTN_GAP = 16
BTN_RADIUS = 0.2

BG_COLOR = rl.Color(22, 33, 62, 220)
BG_DISABLED = rl.Color(22, 33, 62, 100)
BORDER_COLOR = rl.Color(60, 60, 100, 255)
BORDER_ACTIVE = rl.Color(233, 69, 96, 200)
TEXT_COLOR = rl.WHITE
TEXT_DISABLED = rl.Color(120, 120, 120, 255)
ICON_COLOR = rl.Color(233, 69, 96, 255)
ICON_DISABLED = rl.Color(80, 80, 80, 255)

_PARAMS_REFRESH_INTERVAL = 5.0  # seconds between param reads


def _draw_home_icon(cx: float, cy: float, size: float, color: rl.Color) -> None:
  """Draw a simple house icon."""
  s = size
  # Roof triangle
  rl.draw_triangle(
    rl.Vector2(cx, cy - s * 0.5),
    rl.Vector2(cx - s * 0.5, cy),
    rl.Vector2(cx + s * 0.5, cy),
    color,
  )
  # Body rectangle
  body_w = s * 0.7
  body_h = s * 0.42
  rl.draw_rectangle_rec(
    rl.Rectangle(cx - body_w / 2, cy, body_w, body_h),
    color,
  )
  # Door (small cutout) — draw dark rectangle over body
  door_w = body_w * 0.32
  door_h = body_h * 0.55
  rl.draw_rectangle_rec(
    rl.Rectangle(cx - door_w / 2, cy + body_h - door_h, door_w, door_h),
    rl.Color(0, 0, 0, 180),
  )


def _draw_work_icon(cx: float, cy: float, size: float, color: rl.Color) -> None:
  """Draw a simple building/work icon: grid of windows."""
  s = size
  bw = s * 0.9
  bh = s * 0.85
  bx = cx - bw / 2
  by = cy - bh / 2
  # Building outline
  rl.draw_rectangle_rec(rl.Rectangle(bx, by, bw, bh), color)
  # Windows (dark rectangles)
  win_w = bw * 0.22
  win_h = bh * 0.18
  gap_x = bw * 0.18
  gap_y = bh * 0.12
  start_x = bx + gap_x
  start_y = by + gap_y
  for row in range(3):
    for col in range(3):
      wx = start_x + col * (win_w + gap_x)
      wy = start_y + row * (win_h + gap_y)
      rl.draw_rectangle_rec(rl.Rectangle(wx, wy, win_w, win_h), rl.Color(0, 0, 0, 200))


def _draw_refresh_icon(cx: float, cy: float, size: float, color: rl.Color) -> None:
  """Draw a circular refresh arrow."""
  s = size
  r_outer = s * 0.46
  r_inner = s * 0.28
  # Arc ~300 degrees
  rl.draw_ring(rl.Vector2(cx, cy), r_inner, r_outer, -150, 150, 30, color)
  # Arrowhead at end of arc
  end_angle = math.radians(150)
  ax = cx + r_outer * math.cos(end_angle)
  ay = cy + r_outer * math.sin(end_angle)
  rl.draw_triangle(
    rl.Vector2(ax - s * 0.15, ay),
    rl.Vector2(ax + s * 0.06, ay - s * 0.22),
    rl.Vector2(ax + s * 0.06, ay + s * 0.05),
    color,
  )


class NavQuickAccess(Widget):
  def __init__(self):
    super().__init__()
    self.set_visible(False)
    self.has_home = False
    self.has_work = False
    self.font = gui_app.font(FontWeight.BOLD)
    self._params = Params()
    self._last_params_check: float = 0.0
    self._button_hitboxes: list[tuple[str, bool, rl.Rectangle]] = []

  def update(self):
    sm = ui_state.sm
    if not ui_state.started:
      self.set_visible(False)
      return

    try:
      gear = sm["carState"].gearShifter
      in_drive = gear == GearShifter.drive
    except Exception:
      in_drive = False

    self.set_visible(not in_drive)

    # Throttle param reads to avoid reading every frame
    now = time.monotonic()
    if now - self._last_params_check >= _PARAMS_REFRESH_INTERVAL:
      self._last_params_check = now
      home_raw = self._safe_get("NavHomeLocation")
      work_raw = self._safe_get("NavWorkLocation")
      self.has_home = home_raw is not None and home_raw != b""
      self.has_work = work_raw is not None and work_raw != b""

  def _safe_get(self, key: str):
    try:
      return self._params.get(key)
    except UnknownKeyName:
      return None
    except Exception:
      return None

  def _safe_put_json(self, key: str, data: dict) -> bool:
    try:
      self._params.put(key, json.dumps(data))
      return True
    except UnknownKeyName:
      return False
    except Exception:
      return False

  def _safe_remove(self, key: str) -> bool:
    try:
      self._params.remove(key)
      return True
    except UnknownKeyName:
      return False
    except Exception:
      return False

  def _render(self, rect: rl.Rectangle) -> None:
    total_w = 3 * BTN_W + 2 * BTN_GAP
    start_x = rect.x + (rect.width - total_w) / 2
    btn_y = rect.y + rect.height - BTN_H - 80

    buttons = [
      ("Home", "home", self.has_home),
      ("Refresh", "refresh", True),
      ("Work", "work", self.has_work),
    ]

    self._button_hitboxes = []

    for i, (label, icon_type, enabled) in enumerate(buttons):
      bx = start_x + i * (BTN_W + BTN_GAP)
      brect = rl.Rectangle(bx, btn_y, BTN_W, BTN_H)
      self._button_hitboxes.append((icon_type, enabled, brect))

      bg = BG_COLOR if enabled else BG_DISABLED
      border = BORDER_ACTIVE if enabled else BORDER_COLOR
      icon_col = ICON_COLOR if enabled else ICON_DISABLED
      text_col = TEXT_COLOR if enabled else TEXT_DISABLED

      rl.draw_rectangle_rounded(brect, BTN_RADIUS, 10, bg)
      rl.draw_rectangle_rounded_lines_ex(brect, BTN_RADIUS, 10, 2, border)

      icon_cx = bx + BTN_W / 2
      icon_cy = btn_y + BTN_H * 0.38
      icon_size = 28

      if icon_type == "home":
        _draw_home_icon(icon_cx, icon_cy, icon_size, icon_col)
      elif icon_type == "work":
        _draw_work_icon(icon_cx, icon_cy, icon_size, icon_col)
      elif icon_type == "refresh":
        _draw_refresh_icon(icon_cx, icon_cy, icon_size, icon_col)

      label_w = measure_text_cached(self.font, label, 26).x
      rl.draw_text_ex(
        self.font,
        label,
        rl.Vector2(bx + (BTN_W - label_w) / 2, btn_y + BTN_H * 0.68),
        26,
        0,
        text_col,
      )

  def _handle_mouse_release(self, mouse_pos) -> None:
    for icon_type, enabled, brect in self._button_hitboxes:
      if enabled and rl.check_collision_point_rec(mouse_pos, brect):
        self._on_tap(icon_type)
        break

  def _on_tap(self, icon_type: str) -> None:
    if icon_type == "home":
      raw = self._safe_get("NavHomeLocation")
      if raw:
        try:
          data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
          self._safe_put_json("NavDestination", data)
        except Exception:
          pass
    elif icon_type == "work":
      raw = self._safe_get("NavWorkLocation")
      if raw:
        try:
          data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
          self._safe_put_json("NavDestination", data)
        except Exception:
          pass
    elif icon_type == "refresh":
      # Force re-route by toggling destination: clear then re-write
      raw = self._safe_get("NavDestination")
      if raw:
        try:
          data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
          if self._safe_remove("NavDestination"):
            self._safe_put_json("NavDestination", data)
        except Exception:
          pass
