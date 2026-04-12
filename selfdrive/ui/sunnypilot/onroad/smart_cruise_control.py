"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl

from openpilot.selfdrive.ui.onroad.hud_renderer import COLORS
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.sunnypilot.lib.utils import AlertFadeAnimator
from openpilot.system.ui.widgets import Widget


class SmartCruiseControlRenderer(Widget):
  def __init__(self):
    super().__init__()
    self.vision_enabled = False
    self.vision_active = False
    self.vision_gas_gating = False
    self.vision_a_target = 0.0
    self.map_enabled = False
    self.map_active = False
    self.map_gas_gating = False
    self.map_a_target = 0.0
    self.long_override = False

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
      self.vision_a_target = vision.aTarget
      self.map_enabled = map_.enabled
      self.map_active = map_.active
      self.map_gas_gating = map_.gasGating
      self.map_a_target = map_.aTarget

    if sm.updated["carControl"]:
      self.long_override = sm["carControl"].cruiseControl.override

  def _draw_icon(self, rect_center_x, rect_height, x_offset, y_offset, name, is_active, is_gas_gating, a_target):
    # Hide badge when inactive
    if not is_active:
      return

    text = name
    font_size = 36
    padding_v = 5
    box_width = 160

    sz = measure_text_cached(self.font, text, font_size)
    box_height = int(sz.y + padding_v * 2)

    if self.long_override:
      color = COLORS.OVERRIDE
      box_color = rl.Color(color.r, color.g, color.b, 230)
    elif is_gas_gating:
      box_color = rl.Color(255, 140, 0, 230)
    elif a_target < -0.1:
      box_color = rl.Color(220, 50, 50, 230)
    else:
      box_color = rl.Color(220, 50, 50, 230)

    text_color = rl.Color(0, 0, 0, 255)

    screen_y = rect_height / 4 + y_offset

    box_x = rect_center_x + x_offset - box_width / 2
    box_y = screen_y - box_height / 2

    rl.draw_rectangle_rounded(rl.Rectangle(box_x, box_y, box_width, box_height), 0.2, 10, box_color)

    text_pos_x = box_x + (box_width - sz.x) / 2
    text_pos_y = box_y + (box_height - sz.y) / 2

    rl.draw_text_ex(self.font, text, rl.Vector2(text_pos_x, text_pos_y), font_size, 0, text_color)

  def _render(self, rect: rl.Rectangle):
    x_offset = -260
    y1_offset = -40
    y2_offset = -100

    orders = [y1_offset, y2_offset]
    y_scc_v = 0
    y_scc_m = 0
    idx = 0

    if self.vision_enabled:
      y_scc_v = orders[idx]
      idx += 1

    if self.map_enabled:
      y_scc_m = orders[idx]
      idx += 1

    if self.vision_enabled:
      self._draw_icon(rect.x + rect.width / 2, rect.height, x_offset, y_scc_v, "SCC-V",
                      self.vision_active, self.vision_gas_gating, self.vision_a_target)

    if self.map_enabled:
      self._draw_icon(rect.x + rect.width / 2, rect.height, x_offset, y_scc_m, "SCC-M",
                      self.map_active, self.map_gas_gating, self.map_a_target)
