"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.sunnypilot.onroad.developer_ui.elements import (
  UiElement,
  SccCornerSpeedElement, SccGateElement, SccCapElement, SccAuthorityElement,
  SccVisionCapElement, SccCorroborationElement,
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
  # ONE BOTTOM ROW AND ONE RIGHT COLUMN. THAT IS THE WHOLE PANEL, AND IT IS A
  # BUDGET RATHER THAN A DEFAULT — eleven slots, and every one of them has to
  # earn its place against the road it is drawn over.
  #
  # A second row was tried in v3.6.7 and REMOVED at the owner's instruction. It
  # is worth recording why the trade is real in both directions: a row divides
  # a FIXED width between however many elements it is handed, so the only two
  # ways to add a readout are to shrink every other readout or to take more of
  # the windscreen. The answer here is neither — it is to show fewer things.
  #
  # WHAT WENT, AND THE COST, STATED PLAINLY: the SCC-M v2 LEARNING instruments
  # (CORN, R, DIST, ALAT, VIS, PASS, LANE, LRN/NPAS/ORPH). Those diagnose
  # whether the corner store is filling and whether the geometry is finding
  # bends — real questions, and v3.6.4/v3.6.5 added several of them for exactly
  # that. They are not longitudinal BEHAVIOUR, which is what this panel is now
  # for. The data is still published on `fp_sccdbg` and the elements are one
  # commit away if a learning question comes back.
  ROW_H = 61
  BOTTOM_ROWS = 1
  BOTTOM_BAR_HEIGHT = ROW_H * BOTTOM_ROWS

  def __init__(self):
    super().__init__()
    self._font_bold: rl.Font = gui_app.font(FontWeight.BOLD)
    self._font_semi_bold: rl.Font = gui_app.font(FontWeight.SEMI_BOLD)
    self.dev_ui_mode = self.DEV_UI_OFF

    # FunnyPilot v3.6.7 — THE PANEL DIAGNOSES LONGITUDINAL BEHAVIOUR AND
    # NOTHING ELSE. v3.6.2 made it SCC-M v2's instrument, the way v3.3.8 had
    # made it the turn-in oscillation's; both times the right move at the end
    # was to delete the numbers whose question had been answered rather than to
    # keep accumulating rows. Eleven readouts, each one naming a failure this
    # release can actually produce.
    self.scc_corner_speed = SccCornerSpeedElement()
    self.scc_gate = SccGateElement()
    self.scc_cap = SccCapElement()
    self.scc_authority = SccAuthorityElement()
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

    # THE RIGHT COLUMN IS THE CORNER ARGUMENT, TOP TO BOTTOM: the speed we chose
    # for the bend, what SCC-M is asking of the car, what SCC-V is asking, the
    # model's own reading of the road, and how much of SCC-M's ask survived.
    #
    # IT READS AS ONE SENTENCE AND THAT IS THE POINT after v3.6.7. CVSP and CAP
    # are SCC-M; SCCV and CORR are SCC-V; AUTH is the verdict between them. A
    # low CORR with a live CAP and AUTH 0 is the vision veto doing exactly what
    # this release added it for — bad map data at a merge, overruled by a
    # camera looking down an empty road.
    #
    # Fixed order and fixed length — the column must not reflow, or a value
    # dropping out slides every other one and the screen becomes unreadable at
    # a glance (the v3.5.9 station rule).
    elements = [
      self.scc_corner_speed.update(sm, ui_state.is_metric),
      self.scc_cap.update(sm, ui_state.is_metric),
      self.scc_vision_cap.update(sm, ui_state.is_metric),
      self.scc_corroboration.update(sm, ui_state.is_metric),
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

    # THE BOTTOM ROW IS THE LONGITUDINAL LOOP, LEFT TO RIGHT IN THE ORDER THE
    # CAUSE RUNS: who is deciding, what they asked for, whether the actuator
    # delivered it, what we are following, and whether the stop governor is in.
    # SRC/ACC/TRK read together as the whole of v3.6.7 — a moving ACC with TRK
    # near zero is the actuator keeping up with the plan, which is precisely
    # what the predictive-tuning feed-forward was added to make true.
    #
    # UNCONDITIONAL, EVERY FRAME, FIXED ORDER. A row built behind an `if`
    # re-centres itself whenever a source goes quiet, and a readout that moves
    # is a readout you stop trusting (the v3.5.9 station rule). Nothing here
    # may drop out; an element with nothing to say prints "-" and holds its
    # slot.
    elements = [
      self.long_source.update(sm, m),
      self.long_accel.update(sm, m),
      self.long_tracking.update(sm, m),
      self.lead.update(sm, m),
      self.stop_gov.update(sm, m),
      self.scc_gate.update(sm, m),
    ]

    font_size = 38
    element_widths = []
    for element in elements:
      element.measure(self._font_bold, font_size)
      element_widths.append(element.total_width)

    total_element_width = sum(element_widths)
    num_gaps = len(elements) + 1
    gap_width = (rect.width - total_element_width) / num_gaps

    center_y = y + self.ROW_H // 2
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
