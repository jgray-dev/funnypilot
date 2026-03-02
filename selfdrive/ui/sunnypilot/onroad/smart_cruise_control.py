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


class SmartCruiseControlRenderer(Widget):
  def __init__(self):
    super().__init__()
    self.vision_enabled = False
    self.vision_active = False
    self.vision_gas_gating = False
    self.map_enabled = False
    self.map_active = False
    self.map_gas_gating = False

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
      self.map_enabled = map_.enabled
      self.map_active = map_.active
      self.map_gas_gating = map_.gasGating

  def _draw_icon(self, rect_center_x, rect_height, x_offset, y_offset, name, gas_gating=False):
    font_size = 36
    padding_v = 5
    box_width = 160

    sz = measure_text_cached(self.font, name, font_size)
    box_height = int(sz.y + padding_v * 2)

    # FunnyPilot: orange = gas gating (coasting), red = active braking
    box_color = rl.Color(255, 140, 0, 255) if gas_gating else rl.Color(220, 30, 30, 255)

    box_x = rect_center_x + x_offset - box_width / 2
    box_y = rect_height / 4 + y_offset - box_height / 2

    rl.draw_rectangle_rounded(rl.Rectangle(box_x, box_y, box_width, box_height), 0.2, 10, box_color)

    rl.draw_text_ex(self.font, name, rl.Vector2(box_x + (box_width - sz.x) / 2, box_y + (box_height - sz.y) / 2), font_size, 0, rl.BLACK)

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

    # FunnyPilot: only visible when active (red) or gas gating (orange)
    if self.vision_enabled and self.vision_active:
      self._draw_icon(rect.x + rect.width / 2, rect.height, x_offset, y_scc_v, "SCC-V", gas_gating=self.vision_gas_gating)

    if self.map_enabled and self.map_active:
      self._draw_icon(rect.x + rect.width / 2, rect.height, x_offset, y_scc_m, "SCC-M", gas_gating=self.map_gas_gating)
