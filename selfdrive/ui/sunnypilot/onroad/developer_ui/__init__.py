"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.sunnypilot.onroad.developer_ui.elements import (
  UiElement,
  SccCornersElement, SccRadiusElement, SccCornerSpeedElement, SccDistanceElement,
  SccALatElement, SccVisitsElement, SccGateElement, SccCapElement,
  SccAuthorityElement, SccLearnedElement, SccLastPassElement, SccPassCountElement,
)
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

# v3.5.0 right-column geometry, UNCHANGED from v3.4.9 and deliberately so.
# An earlier draft pushed this column down to sit under the route minimap;
# on a 1020 px content area that put the five elements at a 104 px pitch when
# each is 120 px tall, so they overlapped each other, ran into the bottom rail
# and the last one fell off the screen. The minimap moved sideways instead —
# see MAP_RIGHT_INSET in hud_renderer.py, which is keyed off these numbers.
RIGHT_COL_WIDTH = 184
RIGHT_COL_MARGIN = 40
RIGHT_TOP_OFFSET = 230
RIGHT_PITCH = 130


class DeveloperUiRenderer(Widget):
  DEV_UI_OFF = 0
  DEV_UI_BOTTOM = 1
  DEV_UI_RIGHT = 2
  DEV_UI_BOTH = 3
  BOTTOM_BAR_HEIGHT = 61

  def __init__(self):
    super().__init__()
    self._font_bold: rl.Font = gui_app.font(FontWeight.BOLD)
    self._font_semi_bold: rl.Font = gui_app.font(FontWeight.SEMI_BOLD)
    self.dev_ui_mode = self.DEV_UI_OFF

    # FunnyPilot v3.6.2 — the dev UI is SCC-M v2's instrument panel now. The
    # v3.3.8 turn-in oscillation elements (EPS/LIM/TBAR/BUMP) and the lateral
    # readouts went with the investigation that closed; keeping a screen full
    # of numbers nobody reads is how a debug tool stops being one.
    self.scc_corners = SccCornersElement()
    self.scc_radius = SccRadiusElement()
    self.scc_corner_speed = SccCornerSpeedElement()
    self.scc_distance = SccDistanceElement()
    self.scc_a_lat = SccALatElement()
    self.scc_visits = SccVisitsElement()
    self.scc_gate = SccGateElement()
    self.scc_cap = SccCapElement()
    self.scc_authority = SccAuthorityElement()
    self.scc_learned = SccLearnedElement()
    self.scc_last_pass = SccLastPassElement()
    self.scc_pass_count = SccPassCountElement()

  def _draw_right_dev_ui(self, rect: rl.Rectangle) -> None:
    sm = ui_state.sm

    UI_BORDER_SIZE = 20
    container_width = RIGHT_COL_WIDTH
    x = int(rect.x + rect.width - container_width - RIGHT_COL_MARGIN)
    y = int(rect.y + UI_BORDER_SIZE * 1.5)

    # THE RIGHT COLUMN IS THE CORNER WE ARE BRAKING FOR: what we measured, what
    # speed that implies, how far away it is, and how much of it survived the
    # fusion. Fixed order and fixed length — the column must not reflow, or a
    # value dropping out slides every other one and the screen becomes
    # unreadable at a glance (the v3.5.9 station rule).
    elements = [
      self.scc_radius.update(sm, ui_state.is_metric),
      self.scc_corner_speed.update(sm, ui_state.is_metric),
      self.scc_distance.update(sm, ui_state.is_metric),
      self.scc_cap.update(sm, ui_state.is_metric),
      self.scc_authority.update(sm, ui_state.is_metric),
    ]

    current_y = y + RIGHT_TOP_OFFSET
    for element in elements:
      current_y += self._draw_right_dev_ui_element(x, current_y, element)

  def _draw_right_dev_ui_element(self, x: int, y: int, element: UiElement) -> int:
    container_width = 184
    label_size = 28
    value_size = 60
    unit_size = 28
    label_width = measure_text_cached(self._font_bold, element.label, label_size, 0).x
    centered_label_x = x + (container_width - label_width) / 2
    rl.draw_text_ex(self._font_bold, element.label, rl.Vector2(centered_label_x, y), label_size, 0, rl.WHITE)

    y += 45
    value_width = measure_text_cached(self._font_bold, element.value, value_size, 0).x
    centered_value_x = x + (container_width - value_width) / 2
    rl.draw_text_ex(self._font_bold, element.value, rl.Vector2(centered_value_x, y), value_size, 0, element.color)

    if element.unit:
      units_height = measure_text_cached(self._font_bold, element.unit, unit_size, 0).x

      units_x = x + container_width
      units_y = y + (value_size / 2) + (units_height / 2)

      rl.draw_text_pro(self._font_bold, element.unit, rl.Vector2(units_x, units_y), rl.Vector2(0, 0), -90.0, unit_size, 0, rl.WHITE)

    return RIGHT_PITCH

  def _draw_bottom_dev_ui(self, rect: rl.Rectangle) -> None:
    sm = ui_state.sm
    bar_height = 61
    y = int(rect.y + rect.height - bar_height)

    rl.draw_rectangle(int(rect.x), y, int(rect.width), bar_height,
                      rl.Color(0, 0, 0, 100))

    # THE BOTTOM BAR IS THE LEARNING SIDE. Unconditional, every frame, in a
    # fixed order: a bar built conditionally re-centres itself whenever a source
    # goes quiet, and a readout that moves is a readout you stop trusting.
    elements = [
      self.scc_corners.update(sm, ui_state.is_metric),
      self.scc_a_lat.update(sm, ui_state.is_metric),
      self.scc_visits.update(sm, ui_state.is_metric),
      self.scc_gate.update(sm, ui_state.is_metric),
      self.scc_last_pass.update(sm, ui_state.is_metric),
      self.scc_pass_count.update(sm, ui_state.is_metric),
      self.scc_learned.update(sm, ui_state.is_metric),
    ]

    if not elements:
      return

    font_size = 38
    element_widths = []
    for element in elements:
      element.measure(self._font_bold, font_size)
      element_widths.append(element.total_width)

    total_element_width = sum(element_widths)
    num_gaps = len(elements) + 1
    available_width = rect.width
    gap_width = (available_width - total_element_width) / num_gaps

    center_y = y + bar_height // 2
    current_x = rect.x + gap_width

    for i, element in enumerate(elements):
      element_center_x = int(current_x + element_widths[i] / 2)
      self._draw_bottom_dev_ui_element(element_center_x, center_y, element)
      current_x += element_widths[i] + gap_width

  def _draw_bottom_dev_ui_element(self, center_x: int, y: int, element: UiElement) -> None:
    font_size = 38
    start_x = center_x - element.total_width / 2

    rl.draw_text_ex(self._font_bold, element.label_text, rl.Vector2(start_x, y - font_size // 2), font_size, 0, rl.WHITE)
    rl.draw_text_ex(self._font_bold, element.val_text, rl.Vector2(start_x + element.label_width, y - font_size // 2), font_size, 0, element.color)

    if element.unit:
      rl.draw_text_ex(self._font_bold, element.unit_text, rl.Vector2(start_x + element.label_width + element.val_width, y - font_size // 2),
                      font_size, 0, rl.WHITE)
