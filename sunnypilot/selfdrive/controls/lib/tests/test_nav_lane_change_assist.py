from cereal import log

from openpilot.common.constants import CV
from openpilot.sunnypilot.selfdrive.controls.lib.auto_lane_change import AutoLaneChangeMode
from openpilot.sunnypilot.selfdrive.controls.lib.nav_lane_change_assist import NavLaneChangeAssist


class DummyLane:
  def __init__(self, active=False, active_direction="none", directions=None):
    self.active = active
    self.activeDirection = active_direction
    self.directions = directions or ["none"]


class DummyNavInstruction:
  def __init__(self, maneuver_type="", maneuver_modifier="straight", maneuver_distance=0.0, lanes=None):
    self.maneuverType = maneuver_type
    self.maneuverModifier = maneuver_modifier
    self.maneuverDistance = maneuver_distance
    self.lanes = lanes or []


def test_nav_lane_change_assist_triggers_for_highway_exit():
  assist = NavLaneChangeAssist()
  nav = DummyNavInstruction(
    maneuver_type="off ramp",
    maneuver_modifier="slight right",
    maneuver_distance=220.0,
    lanes=[DummyLane(active=True, active_direction="right", directions=["slight right"])],
  )

  left, right = assist.update(
    nav,
    v_ego=70.0 * CV.MPH_TO_MS,
    lateral_active=True,
    lane_change_state=log.LaneChangeState.off,
    lane_change_set_timer=AutoLaneChangeMode.ONE_SECOND,
    manual_one_blinker=False,
  )
  assert not left
  assert right


def test_nav_lane_change_assist_respects_auto_lane_change_mode():
  assist = NavLaneChangeAssist()
  nav = DummyNavInstruction(maneuver_type="off ramp", maneuver_modifier="right", maneuver_distance=180.0)

  left, right = assist.update(
    nav,
    v_ego=70.0 * CV.MPH_TO_MS,
    lateral_active=True,
    lane_change_state=log.LaneChangeState.off,
    lane_change_set_timer=AutoLaneChangeMode.NUDGE,
    manual_one_blinker=False,
  )
  assert not left
  assert not right


def test_nav_lane_change_assist_triggers_once_per_maneuver():
  assist = NavLaneChangeAssist()
  nav = DummyNavInstruction(maneuver_type="merge", maneuver_modifier="left", maneuver_distance=160.0)

  first = assist.update(
    nav,
    v_ego=65.0 * CV.MPH_TO_MS,
    lateral_active=True,
    lane_change_state=log.LaneChangeState.off,
    lane_change_set_timer=AutoLaneChangeMode.HALF_SECOND,
    manual_one_blinker=False,
  )

  second = assist.update(
    nav,
    v_ego=65.0 * CV.MPH_TO_MS,
    lateral_active=True,
    lane_change_state=log.LaneChangeState.off,
    lane_change_set_timer=AutoLaneChangeMode.HALF_SECOND,
    manual_one_blinker=False,
  )

  assert first == (True, False)
  assert second == (True, False)

  # Simulate the next maneuver entering view by a large distance jump.
  nav_next = DummyNavInstruction(maneuver_type="merge", maneuver_modifier="left", maneuver_distance=280.0)
  third = assist.update(
    nav_next,
    v_ego=65.0 * CV.MPH_TO_MS,
    lateral_active=True,
    lane_change_state=log.LaneChangeState.off,
    lane_change_set_timer=AutoLaneChangeMode.HALF_SECOND,
    manual_one_blinker=False,
  )
  assert third == (True, False)
