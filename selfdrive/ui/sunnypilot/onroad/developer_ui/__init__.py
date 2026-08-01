"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.sunnypilot.onroad.developer_ui.elements import (
  UiElement, RelDistElement, RelSpeedElement, SteeringAngleElement,
  DesiredLateralAccelElement, ActualLateralAccelElement, DesiredSteeringAngleElement,
  AEgoElement, FrictionCoefficientElement, LatAccelFactorElement,
  SteeringTorqueEpsElement, BearingDegElement, AltitudeElement, DesiredSteeringPIDElement,
  EpsLimitElement, DriverTorqueElement, TorqueLimitActiveElement, SuspensionBumpElement, LagdElement,
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

    self.rel_dist_elem = RelDistElement()
    self.rel_speed_elem = RelSpeedElement()
    self.steering_angle_elem = SteeringAngleElement()
    self.desired_lat_accel_elem = DesiredLateralAccelElement()
    self.actual_lat_accel_elem = ActualLateralAccelElement()
    self.desired_steer_elem = DesiredSteeringAngleElement()
    self.desired_pid_steer_elem = DesiredSteeringPIDElement()
    self.a_ego_elem = AEgoElement()
    self.friction_elem = FrictionCoefficientElement()
    self.lat_accel_factor_elem = LatAccelFactorElement()
    # FunnyPilot v3.3.8: INTERP replaced — the interpolation is knot-exact by
    # construction now, so it read a static "5". These show what actually
    # matters for the turn-in oscillation: hardware torque authority ceiling,
    # whether that ceiling is actively biting the request right now, the raw
    # torsion-bar reading that drives the clamp, and the bump/pitch-rate
    # hypothesis signal.
    self.eps_limit_elem = EpsLimitElement()
    self.torque_limit_active_elem = TorqueLimitActiveElement()
    self.driver_torque_elem = DriverTorqueElement()
    self.bump_elem = SuspensionBumpElement()
    self.lagd_elem = LagdElement()
    self.steering_torque_elem = SteeringTorqueEpsElement()
    self.bearing_elem = BearingDegElement()
    self.altitude_elem = AltitudeElement()

  @staticmethod
  def get_bottom_dev_ui_offset():
    if ui_state.developer_ui in (DeveloperUiRenderer.DEV_UI_BOTTOM, DeveloperUiRenderer.DEV_UI_BOTH):
      return DeveloperUiRenderer.BOTTOM_BAR_HEIGHT
    return 0

  def _update_state(self) -> None:
    self.dev_ui_mode = ui_state.developer_ui

  def _render(self, rect: rl.Rectangle) -> None:
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
    controls_state = sm['controlsState']

    UI_BORDER_SIZE = 20
    container_width = RIGHT_COL_WIDTH
    x = int(rect.x + rect.width - container_width - RIGHT_COL_MARGIN)
    y = int(rect.y + UI_BORDER_SIZE * 1.5)

    elements = [
      self.rel_dist_elem.update(sm, ui_state.is_metric),
      self.rel_speed_elem.update(sm, ui_state.is_metric),
      self.steering_angle_elem.update(sm, ui_state.is_metric),
    ]
    if controls_state.lateralControlState.which() == 'torqueState':
      elements.append(self.desired_lat_accel_elem.update(sm, ui_state.is_metric))
    elif controls_state.lateralControlState.which() == 'angleState':
      elements.append(self.desired_steer_elem.update(sm, ui_state.is_metric))
    elif controls_state.lateralControlState.which() == 'pidState':
      elements.append(self.desired_pid_steer_elem.update(sm, ui_state.is_metric))

    elements.append(self.actual_lat_accel_elem.update(sm, ui_state.is_metric))

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

    elements = []
    is_torque = sm['controlsState'].lateralControlState.which() == 'torqueState'

    # Leftmost (torque only): the values that discriminate the grab/loosen
    # oscillation hypotheses — authority ceiling, whether it's biting right
    # now, the raw torsion-bar reading, and the bump/pitch-rate signal.
    if is_torque:
      elements.append(self.eps_limit_elem.update(sm, ui_state.is_metric))
      elements.append(self.torque_limit_active_elem.update(sm, ui_state.is_metric))
      elements.append(self.driver_torque_elem.update(sm, ui_state.is_metric))
      elements.append(self.bump_elem.update(sm, ui_state.is_metric))

    if sm.valid['liveDelay']:
      elements.append(self.lagd_elem.update(sm, ui_state.is_metric))

    if is_torque:
      if sm.valid['liveTorqueParameters']:
        elements.extend([
          self.friction_elem.update(sm, ui_state.is_metric),
          self.lat_accel_factor_elem.update(sm, ui_state.is_metric),
        ])
    else:
      elements.append(self.steering_torque_elem.update(sm, ui_state.is_metric))
      if sm.valid['gpsLocationExternal'] or sm.valid['gpsLocation']:
        elements.append(self.bearing_elem.update(sm, ui_state.is_metric))

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
