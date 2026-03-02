from __future__ import annotations

from cereal import log

from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.auto_lane_change import AutoLaneChangeMode


LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection

AUTO_NAV_MANEUVERS = {"off ramp", "on ramp", "fork", "merge"}

NAV_MIN_SPEED = 40.0 * CV.MPH_TO_MS
NAV_MIN_DISTANCE = 45.0
NAV_MAX_DISTANCE = 420.0
NAV_LOOKAHEAD_TIME = 9.0
NAV_BLINKER_PULSE = 3.0
NAV_TRIGGER_COOLDOWN = 8.0
MANEUVER_RESET_DISTANCE = 80.0


def _normalize_direction(raw) -> str:
  if raw is None:
    return "none"

  if isinstance(raw, int):
    int_map = {
      0: "none",
      1: "left",
      2: "right",
      3: "straight",
      4: "slightleft",
      5: "slightright",
    }
    return int_map.get(raw, "none")

  text = str(raw).strip().lower().replace("-", "")
  if "slightleft" in text:
    return "slightleft"
  if "slightright" in text:
    return "slightright"
  if "left" in text:
    return "left"
  if "right" in text:
    return "right"
  if "straight" in text:
    return "straight"
  return "none"


def _modifier_to_lane_change_direction(modifier: str) -> int:
  direction = _normalize_direction(modifier)
  if direction in ("left", "slightleft"):
    return LaneChangeDirection.left
  if direction in ("right", "slightright"):
    return LaneChangeDirection.right
  return LaneChangeDirection.none


def _lane_hint_direction(nav_instruction) -> int:
  left_votes = 0
  right_votes = 0

  for lane in getattr(nav_instruction, "lanes", []):
    if not getattr(lane, "active", False):
      continue

    active_direction = _normalize_direction(getattr(lane, "activeDirection", "none"))
    if active_direction in ("left", "slightleft"):
      left_votes += 1
      continue
    if active_direction in ("right", "slightright"):
      right_votes += 1
      continue

    for direction in getattr(lane, "directions", []):
      normalized = _normalize_direction(direction)
      if normalized in ("left", "slightleft"):
        left_votes += 1
      elif normalized in ("right", "slightright"):
        right_votes += 1

  if left_votes > right_votes and left_votes > 0:
    return LaneChangeDirection.left
  if right_votes > left_votes and right_votes > 0:
    return LaneChangeDirection.right
  return LaneChangeDirection.none


class NavLaneChangeAssist:
  def __init__(self) -> None:
    self._virtual_direction = LaneChangeDirection.none
    self._virtual_blinker_timer = 0.0
    self._cooldown_timer = 0.0
    self._maneuver_signature = ""
    self._last_maneuver_distance = 0.0
    self._triggered_for_maneuver = False

  def _update_timers(self) -> None:
    self._virtual_blinker_timer = max(0.0, self._virtual_blinker_timer - DT_MDL)
    self._cooldown_timer = max(0.0, self._cooldown_timer - DT_MDL)

  def _reset_maneuver_trigger(self, maneuver_signature: str, maneuver_distance: float) -> None:
    is_new_maneuver = maneuver_signature != self._maneuver_signature
    distance_jumped = maneuver_distance > (self._last_maneuver_distance + MANEUVER_RESET_DISTANCE)
    if is_new_maneuver or distance_jumped:
      self._triggered_for_maneuver = False
      self._maneuver_signature = maneuver_signature

    self._last_maneuver_distance = maneuver_distance

  def update(
    self, nav_instruction, v_ego: float, lateral_active: bool, lane_change_state: int, lane_change_set_timer: int, manual_one_blinker: bool
  ) -> tuple[bool, bool]:
    self._update_timers()

    if lane_change_state == LaneChangeState.laneChangeStarting:
      self._virtual_blinker_timer = 0.0

    if manual_one_blinker:
      self._virtual_blinker_timer = 0.0
      return False, False

    if not lateral_active or lane_change_set_timer <= AutoLaneChangeMode.NUDGE:
      return False, False

    maneuver_type = str(getattr(nav_instruction, "maneuverType", "")).strip().lower()
    maneuver_modifier = str(getattr(nav_instruction, "maneuverModifier", "")).strip().lower()
    maneuver_distance = float(max(0.0, getattr(nav_instruction, "maneuverDistance", 0.0)))

    maneuver_signature = f"{maneuver_type}|{maneuver_modifier}"
    self._reset_maneuver_trigger(maneuver_signature, maneuver_distance)

    desired_direction = _lane_hint_direction(nav_instruction)
    if desired_direction == LaneChangeDirection.none:
      desired_direction = _modifier_to_lane_change_direction(maneuver_modifier)

    max_trigger_distance = min(max(v_ego * NAV_LOOKAHEAD_TIME, 120.0), NAV_MAX_DISTANCE)
    maneuver_supported = maneuver_type in AUTO_NAV_MANEUVERS
    in_distance_window = NAV_MIN_DISTANCE <= maneuver_distance <= max_trigger_distance

    should_trigger = (
      maneuver_supported
      and desired_direction != LaneChangeDirection.none
      and v_ego >= NAV_MIN_SPEED
      and lane_change_state == LaneChangeState.off
      and in_distance_window
      and not self._triggered_for_maneuver
      and self._cooldown_timer <= 0.0
    )

    if should_trigger:
      self._virtual_direction = desired_direction
      self._virtual_blinker_timer = NAV_BLINKER_PULSE
      self._cooldown_timer = NAV_TRIGGER_COOLDOWN
      self._triggered_for_maneuver = True

    if self._virtual_blinker_timer <= 0.0:
      return False, False

    return self._virtual_direction == LaneChangeDirection.left, self._virtual_direction == LaneChangeDirection.right
