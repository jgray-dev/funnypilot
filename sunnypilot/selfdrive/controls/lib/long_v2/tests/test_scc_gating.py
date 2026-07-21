"""FunnyPilot v3.3.8 — SCC-M/SCC-V arbitration: map may only narrow vision's
cap, never introduce a reduction vision doesn't share.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.speed_governor import gate_map_target
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE


class TestGateMapTarget:
  def test_map_alone_is_ignored(self):
    # map wants to slow down for a curve, vision sees nothing -> no effect
    assert gate_map_target(20.0, vision_is_active=False) == CAP_INACTIVE

  def test_map_confirmed_by_vision_passes_through(self):
    # both agree -> map's cap reaches the governor unchanged
    assert gate_map_target(20.0, vision_is_active=True) == 20.0

  def test_map_inactive_stays_inactive_either_way(self):
    assert gate_map_target(CAP_INACTIVE, vision_is_active=True) == CAP_INACTIVE
    assert gate_map_target(CAP_INACTIVE, vision_is_active=False) == CAP_INACTIVE
