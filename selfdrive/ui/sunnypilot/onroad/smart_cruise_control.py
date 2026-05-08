"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget
from openpilot.common.constants import CV


# Badge state anchors
_COLOR_INACTIVE = rl.Color(46, 204, 113, 60)     # green, low alpha
_COLOR_GAS_GATE = rl.Color(243, 156, 18, 90)     # orange
_COLOR_BRAKING  = rl.Color(231, 76, 60, 100)     # red, full
_COLOR_DISABLED = rl.Color(127, 140, 141, 40)    # gray, low alpha

_TRANSITION_FRAMES = 6  # 300ms at 20Hz


def _lerp_color(a: rl.Color, b: rl.Color, t: float) -> rl.Color:
  t = max(0.0, min(1.0, t))
  return rl.Color(
    int(a.r + (b.r - a.r) * t),
    int(a.g + (b.g - a.g) * t),
    int(a.b + (b.b - a.b) * t),
    int(a.a + (b.a - a.a) * t),
  )


class _BadgeState:
  def __init__(self):
    self.color = _COLOR_DISABLED
    self._target = _COLOR_DISABLED
    self._frame = _TRANSITION_FRAMES

  def set_target(self, target: rl.Color) -> None:
    if (target.r, target.g, target.b) != (self._target.r, self._target.g, self._target.b):
      self._from = self.color
      self._target = target
      self._frame = 0

  def tick(self) -> None:
    if self._frame < _TRANSITION_FRAMES:
      self._frame += 1
      t = self._frame / _TRANSITION_FRAMES
      self.color = _lerp_color(self._from, self._target, t)
    else:
      self.color = self._target

  def __init__(self):
    self._from = _COLOR_DISABLED
    self._target = _COLOR_DISABLED
    self._frame = _TRANSITION_FRAMES
    self.color = _COLOR_DISABLED


class SmartCruiseControlRenderer(Widget):
  def __init__(self):
    super().__init__()
    self.vision_enabled = False
    self.vision_active = False
    self.vision_gas_gating = False
    self.map_enabled = False
    self.map_active = False
    self.map_gas_gating = False
    self.map_corner_radius = 0.0
    self.long_override = False
    self.vision_v_target = 999.0
    self.map_v_target = 999.0

    self._vision_badge = _BadgeState()
    self._map_badge = _BadgeState()
    self.font = gui_app.font(FontWeight.BOLD)

  def update(self):
    sm = ui_state.sm
    if sm.updated["longitudinalPlanSP"]:
      lp_sp = sm["longitudinalPlanSP"]
      vision = lp_sp.smartCruiseControl.vision
      map_ = lp_sp.smartCruiseControl.map

      self.vision_enabled = vision.enabled
      self.vision_active = vision.active
      self.vision_gas_gating = vision.gasGating
      self.vision_v_target = vision.vTarget

      self.map_enabled = map_.enabled
      self.map_active = map_.active
      self.map_gas_gating = map_.gasGating
      self.map_v_target = map_.vTarget
      self.map_corner_radius = map_.cornerRadiusAhead

    if sm.updated["carControl"]:
      self.long_override = sm["carControl"].cruiseControl.override

    # Determine target colors
    def _badge_color(enabled, active, gas_gating):
      if not enabled:
        return _COLOR_DISABLED
      if not active:
        return _COLOR_INACTIVE
      if gas_gating:
        return _COLOR_GAS_GATE
      return _COLOR_BRAKING

    self._vision_badge.set_target(_badge_color(self.vision_enabled, self.vision_active, self.vision_gas_gating))
    self._map_badge.set_target(_badge_color(self.map_enabled, self.map_active, self.map_gas_gating))
    self._vision_badge.tick()
    self._map_badge.tick()

  def _draw_badge(self, rect_center_x: float, rect_height: float, x_offset: float, y_offset: float,
                  label: str, badge: _BadgeState, v_target: float, enabled: bool,
                  force_visible: bool = False):
    if not enabled and not force_visible and v_target >= 888.0:
      return

    font_size = 36
    padding_h = 16
    padding_v = 6
    min_width = 120

    # When governing, show speed instead of label
    if v_target < 888.0:
      v_mph = v_target * CV.MS_TO_MPH
      text = f"{v_mph:.0f}"
    else:
      text = label

    sz = measure_text_cached(self.font, text, font_size)
    box_width = max(min_width, int(sz.x + padding_h * 2))
    box_height = int(sz.y + padding_v * 2)

    screen_y = rect_height / 4 + y_offset
    box_x = rect_center_x + x_offset - box_width / 2
    box_y = screen_y - box_height / 2

    # Drop shadow
    shadow = rl.Color(0, 0, 0, 60)
    rl.draw_rectangle_rounded(rl.Rectangle(box_x + 2, box_y + 2, box_width, box_height), 0.25, 10, shadow)

    # Badge background with gradient color
    rl.draw_rectangle_rounded(rl.Rectangle(box_x, box_y, box_width, box_height), 0.25, 10, badge.color)

    # White text centered
    text_x = box_x + (box_width - sz.x) / 2
    text_y = box_y + (box_height - sz.y) / 2
    rl.draw_text_ex(self.font, text, rl.Vector2(text_x, text_y), font_size, 0, rl.WHITE)

    # If showing speed, draw small label below
    if v_target < 888.0:
      sub_font_size = 20
      sub_sz = measure_text_cached(self.font, label, sub_font_size)
      sub_x = box_x + (box_width - sub_sz.x) / 2
      sub_y = box_y + box_height + 2
      rl.draw_text_ex(self.font, label, rl.Vector2(sub_x, sub_y), sub_font_size, 0, rl.Color(255, 255, 255, 180))

  def _render(self, rect: rl.Rectangle):
    x_offset = -260
    y1_offset = -40
    y2_offset = -100

    orders = [y1_offset, y2_offset]
    y_scc_v = y1_offset
    y_scc_m = y1_offset
    idx = 0

    if self.vision_enabled:
      y_scc_v = orders[idx]
      idx += 1

    if self.map_enabled:
      y_scc_m = orders[idx]
      idx += 1

    cx = rect.x + rect.width / 2

    # Debug UI mode: show calculated speeds at all times when DevUIInfo is on
    debug_mode = bool(ui_state.developer_ui)

    if self.vision_enabled:
      if debug_mode:
        v_show = self.vision_v_target  # always show calculated value
      else:
        v_show = self.vision_v_target if self.vision_active else 999.0
      self._draw_badge(cx, rect.height, x_offset, y_scc_v, "SCC-V", self._vision_badge,
                       v_show, self.vision_enabled, debug_mode)

    if self.map_enabled:
      if debug_mode:
        v_show = self.map_v_target
      else:
        v_show = self.map_v_target if self.map_active else 999.0
      self._draw_badge(cx, rect.height, x_offset, y_scc_m, "SCC-M", self._map_badge,
                       v_show, self.map_enabled, debug_mode)

    if debug_mode and not self.vision_enabled and self.vision_v_target < 888.0:
      self._draw_badge(cx, rect.height, x_offset, y_scc_v, "SCC-V", self._vision_badge,
                       self.vision_v_target, self.vision_enabled, True)
    if debug_mode and not self.map_enabled and self.map_v_target < 888.0:
      self._draw_badge(cx, rect.height, x_offset, y_scc_m, "SCC-M", self._map_badge,
                       self.map_v_target, self.map_enabled, True)
