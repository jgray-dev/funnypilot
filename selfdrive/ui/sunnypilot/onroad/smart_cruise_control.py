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
from openpilot.system.ui.widgets import Widget


class SmartCruiseControlRenderer(Widget):
  def __init__(self):
    super().__init__()
    self.vision_enabled = False
    self.vision_active = False
    self.vision_frame = 0
    self.vision_gas_gating = False  # FunnyPilot
    self.map_enabled = False
    self.map_active = False
    self.map_frame = 0
    self.map_gas_gating = False  # FunnyPilot
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
      self.vision_gas_gating = vision.gasGating  # FunnyPilot
      self.map_enabled = map_.enabled
      self.map_active = map_.active
      self.map_gas_gating = map_.gasGating  # FunnyPilot

    if sm.updated["carControl"]:
      self.long_override = sm["carControl"].cruiseControl.override

    if self.vision_active:
      self.vision_frame += 1
    else:
      self.vision_frame = 0

    if self.map_active:
      self.map_frame += 1
    else:
      self.map_frame = 0

  @staticmethod
  def _pulse_element(frame):
    return not (frame % gui_app.target_fps < (gui_app.target_fps / 2.5))

  def _draw_icon(self, rect_center_x, rect_height, x_offset, y_offset, name, gas_gating=False):
    text = name
    font_size = 36
    padding_v = 5
    box_width = 160

    sz = measure_text_cached(self.font, text, font_size)
    box_height = int(sz.y + padding_v * 2)

    if self.long_override:
      box_color = COLORS.OVERRIDE
    elif gas_gating:
      # FunnyPilot: Orange color when gas gating is active
      box_color = rl.Color(255, 140, 0, 255)
    else:
      box_color = rl.Color(0, 255, 0, 255)

    screen_y = rect_height / 4 + y_offset

    box_x = rect_center_x + x_offset - box_width / 2
    box_y = screen_y - box_height / 2

    # Draw rounded background box
    rl.draw_rectangle_rounded(rl.Rectangle(box_x, box_y, box_width, box_height), 0.2, 10, box_color)

    # Draw text centered in the box
    text_pos_x = box_x + (box_width - sz.x) / 2
    text_pos_y = box_y + (box_height - sz.y) / 2
    rl.draw_text_ex(self.font, text, rl.Vector2(text_pos_x, text_pos_y), font_size, 0, rl.BLACK)

    # FunnyPilot: If gas gating, draw "GAS GATE" sub-label below
    if gas_gating:
      sub_text = "GAS GATE"
      sub_font_size = 22
      sub_sz = measure_text_cached(self.font, sub_text, sub_font_size)
      sub_x = box_x + (box_width - sub_sz.x) / 2
      sub_y = box_y + box_height + 2
      rl.draw_text_ex(self.font, sub_text, rl.Vector2(sub_x, sub_y), sub_font_size, 0,
                      rl.Color(255, 140, 0, 200))

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

    scc_vision_pulse = self._pulse_element(self.vision_frame)
    if (self.vision_enabled and not self.vision_active) or (self.vision_active and scc_vision_pulse):
      self._draw_icon(rect.x + rect.width / 2, rect.height, x_offset, y_scc_v, "SCC-V",
                      gas_gating=self.vision_gas_gating)

    scc_map_pulse = self._pulse_element(self.map_frame)
    if (self.map_enabled and not self.map_active) or (self.map_active and scc_map_pulse):
      self._draw_icon(rect.x + rect.width / 2, rect.height, x_offset, y_scc_m, "SCC-M",
                      gas_gating=self.map_gas_gating)
