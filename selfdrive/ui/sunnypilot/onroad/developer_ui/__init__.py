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
  SccAuthorityElement, SccLearnedElement, SccLastPassElement,
  SccLaneDepartElement, SccVisionCapElement, SccCorroborationElement,
  LongSourceElement, LongAccelElement, LongTrackingElement, StopGovernorElement,
  LeadElement,
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
  # FunnyPilot v3.6.7 — TWO ROWS. v3.6.7 moved the actuator's tracking of the
  # plan, the stop-and-go governor and the vision/map fusion, and none of them
  # had a readout; adding five elements to a single bar would have shrunk the
  # nine already there, because `_draw_bottom_dev_ui` divides a FIXED width
  # between however many it is handed.
  #
  # ARITHMETIC, NOT EYE (the v3.5.0 rule). Each row is ROW_H = 61 px, which is
  # what one row has always been, so 2 x 61 = 122. The bottom horizon band is
  # `chrome.BAND_BOT_H` = 138 px, so the whole panel still sits INSIDE the scrim
  # that exists to make text legible over road — 16 px of margin, and the band
  # is what the panel is drawn against rather than something it has to clear.
  # `get_bottom_dev_ui_offset()` returns the full 122, so the long-status dot
  # and the driver-state widget move up by exactly one extra row.
  ROW_H = 61
  BOTTOM_ROWS = 2
  BOTTOM_BAR_HEIGHT = ROW_H * BOTTOM_ROWS

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
    self.scc_lane_depart = SccLaneDepartElement()
    # v3.6.7 — SCC-V beside SCC-M, because after the fusion change the question
    # on this panel is which of the two is right rather than what SCC-M alone
    # is doing; plus the longitudinal row, which is what v3.6.7 actually moved.
    self.scc_vision_cap = SccVisionCapElement()
    self.scc_corroboration = SccCorroborationElement()
    self.long_source = LongSourceElement()
    self.long_accel = LongAccelElement()
    self.long_tracking = LongTrackingElement()
    self.stop_gov = StopGovernorElement()
    self.lead = LeadElement()

  @staticmethod
  def get_bottom_dev_ui_offset() -> int:
    """How much of the bottom of the frame this widget owns.

    RESTORED in v3.6.2 after the rewrite dropped it. `hud_renderer._render`
    and `driver_state` both call it to keep other widgets clear of the bar;
    with it missing they raise AttributeError, which kills the UI process the
    same way the abstract-method error did — just one boot later.
    """
    if ui_state.developer_ui in (DeveloperUiRenderer.DEV_UI_BOTTOM, DeveloperUiRenderer.DEV_UI_BOTH):
      return DeveloperUiRenderer.BOTTOM_BAR_HEIGHT
    return 0

  def _update_state(self) -> None:
    """Widget.render() calls this before _render every frame. Without it
    `dev_ui_mode` never leaves DEV_UI_OFF and the panel silently never draws —
    the quietest of the three defects the rewrite introduced, and the only one
    that would not have crashed."""
    self.dev_ui_mode = ui_state.developer_ui

  def _render(self, rect: rl.Rectangle) -> None:
    """THE ABSTRACT METHOD `Widget` REQUIRES. Its absence is what put the
    device in a boot loop on the first flash of v3.6.2: `Widget` is an
    `abc.ABC` whose public `render()` dispatches here, so a subclass without
    it cannot be INSTANTIATED at all. The rewrite replaced the body with
    `_draw_right_dev_ui`/`_draw_bottom_dev_ui` and never re-declared the
    method those two are dispatched from.

    Note this is not an import error, which is why every import guard in the
    package passed — see test_hud_widgets_are_concrete.
    """
    if self.dev_ui_mode == self.DEV_UI_OFF:
      return

    sm = ui_state.sm
    if sm.recv_frame["carState"] < ui_state.started_frame:
      return

    if self.dev_ui_mode == self.DEV_UI_BOTTOM:
      self._draw_bottom_dev_ui(rect)
    elif self.dev_ui_mode == self.DEV_UI_RIGHT:
      self._draw_right_dev_ui(rect)
    elif self.dev_ui_mode == self.DEV_UI_BOTH:
      self._draw_right_dev_ui(rect)
      self._draw_bottom_dev_ui(rect)

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
    m = ui_state.is_metric
    y = int(rect.y + rect.height - self.BOTTOM_BAR_HEIGHT)

    rl.draw_rectangle(int(rect.x), y, int(rect.width), self.BOTTOM_BAR_HEIGHT,
                      rl.Color(0, 0, 0, 100))

    # TWO ROWS, BOTH UNCONDITIONAL, BOTH IN A FIXED ORDER. A row built
    # conditionally re-centres itself whenever a source goes quiet, and a
    # readout that moves is a readout you stop trusting (the v3.5.9 station
    # rule). Nothing here is allowed to drop out; an element with nothing to
    # say prints "-" and holds its slot.
    #
    # TOP ROW — WHAT IS DECIDING THE LONGITUDINAL, which is what v3.6.7
    # changed. Deliberately the sparser of the two: it is the row you read at a
    # glance while driving, and it earns the width.
    #
    # BOTTOM ROW — SCC. Vision's own cap and the model's own corroboration sit
    # at the head of it, ahead of SCC-M's numbers, because after v3.6.7 vision
    # is what decides whether any of the rest gets to act.
    rows = [
      [
        self.long_source.update(sm, m),
        self.long_accel.update(sm, m),
        self.long_tracking.update(sm, m),
        self.lead.update(sm, m),
        self.stop_gov.update(sm, m),
      ],
      [
        self.scc_vision_cap.update(sm, m),
        self.scc_corroboration.update(sm, m),
        self.scc_corners.update(sm, m),
        self.scc_a_lat.update(sm, m),
        self.scc_visits.update(sm, m),
        self.scc_gate.update(sm, m),
        self.scc_last_pass.update(sm, m),
        self.scc_lane_depart.update(sm, m),
        self.scc_learned.update(sm, m),
      ],
    ]

    for row_i, elements in enumerate(rows):
      if not elements:
        continue
      self._draw_bottom_row(rect, y + row_i * self.ROW_H, elements)

  def _draw_bottom_row(self, rect: rl.Rectangle, row_y: int, elements: list) -> None:
    font_size = 38
    element_widths = []
    for element in elements:
      element.measure(self._font_bold, font_size)
      element_widths.append(element.total_width)

    total_element_width = sum(element_widths)
    num_gaps = len(elements) + 1
    gap_width = (rect.width - total_element_width) / num_gaps

    center_y = row_y + self.ROW_H // 2
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
