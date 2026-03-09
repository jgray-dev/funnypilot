"""
NavState: route progress state machine.
Tracks position on route, advances step index, detects arrival/off-route.
"""

from __future__ import annotations
import math
from openpilot.sunnypilot.navd.helpers import Coordinate, distance_along_geometry


OFF_ROUTE_THRESHOLD_M = 100.0  # meters before triggering re-route
ARRIVAL_THRESHOLD_M = 30.0  # meters from destination = arrived


class NavState:
  def __init__(self):
    self.steps: list[dict] = []
    self.geometry_coords: list[Coordinate] = []
    self.step_index: int = 0
    self.destination: Coordinate | None = None
    self.dest_name: str = ""
    self.dest_addr: str = ""

    self.distance_to_maneuver: float = 0.0
    self.distance_remaining: float = 0.0
    self.time_remaining: float = 0.0
    self.total_route_duration: float = 0.0
    self.total_route_distance: float = 0.0
    self._min_dist_to_maneuver: float = 100000.0

  def set_route(self, route: dict, dest_lat: float, dest_lon: float, dest_name: str = "", dest_addr: str = "") -> None:
    """Load a new route from OSRM response."""
    self.steps = route.get("steps", [])
    self.step_index = 0
    self.destination = Coordinate(dest_lat, dest_lon)
    self.dest_name = dest_name
    self.dest_addr = dest_addr
    self.total_route_distance = route.get("distance", 0.0)
    self.total_route_duration = route.get("duration", 0.0)
    self.distance_remaining = self.total_route_distance
    self.time_remaining = self.total_route_duration
    self.distance_to_maneuver = 0.0
    self._min_dist_to_maneuver = 100000.0

    coords = route.get("geometry", {}).get("coordinates", [])
    self.geometry_coords = [Coordinate(c[1], c[0]) for c in coords]

  def clear(self) -> None:
    self.steps = []
    self.geometry_coords = []
    self.step_index = 0
    self.destination = None
    self.dest_name = ""
    self.dest_addr = ""
    self.distance_to_maneuver = 0.0
    self.distance_remaining = 0.0
    self.time_remaining = 0.0
    self._min_dist_to_maneuver = 100000.0

  @property
  def active(self) -> bool:
    return self.destination is not None and len(self.steps) > 0

  def update(self, lat: float, lon: float) -> None:
    """Update position and advance step index as needed."""
    if not self.active:
      return

    pos = Coordinate(lat, lon)

    # Update distance/time remaining based on progress along route geometry
    if self.geometry_coords:
      dist_along = distance_along_geometry(self.geometry_coords, pos)
      self.distance_remaining = max(0.0, self.total_route_distance - dist_along)
      fraction_done = min(1.0, dist_along / self.total_route_distance) if self.total_route_distance > 0 else 0.0
      self.time_remaining = max(0.0, self.total_route_duration * (1.0 - fraction_done))

    # Advance step index
    while self.step_index < len(self.steps) - 1:
      step = self.steps[self.step_index]
      maneuver_loc = step["maneuver"]["location"]  # [lon, lat]
      maneuver_coord = Coordinate(maneuver_loc[1], maneuver_loc[0])
      dist_to_maneuver = pos.distance_to(maneuver_coord)

      # If we are within 35m, or we got within 150m and are now moving away (passed it)
      passed_maneuver = False
      if dist_to_maneuver < 35.0:
        passed_maneuver = True
      elif self._min_dist_to_maneuver < 150.0 and dist_to_maneuver > self._min_dist_to_maneuver + 25.0:
        passed_maneuver = True

      if passed_maneuver:
        self.step_index += 1
        self._min_dist_to_maneuver = 100000.0
      else:
        self._min_dist_to_maneuver = min(self._min_dist_to_maneuver, dist_to_maneuver)
        self.distance_to_maneuver = dist_to_maneuver
        break
    else:
      if self.step_index < len(self.steps):
        step = self.steps[self.step_index]
        maneuver_loc = step["maneuver"]["location"]
        self.distance_to_maneuver = pos.distance_to(Coordinate(maneuver_loc[1], maneuver_loc[0]))

  def current_instruction(self) -> dict:
    """Return dict ready to fill navInstruction cereal fields."""
    if not self.active or self.step_index >= len(self.steps):
      return {}

    step = self.steps[self.step_index]
    maneuver = step.get("maneuver", {})

    def _safe_float(value, default: float = 0.0) -> float:
      try:
        return float(value)
      except Exception:
        return default

    lanes = step.get("lanes", [])
    if not isinstance(lanes, list):
      lanes = []

    all_maneuvers: list[dict] = []
    cumulative_distance = max(0.0, _safe_float(self.distance_to_maneuver))
    max_upcoming = min(len(self.steps), self.step_index + 6)
    for idx in range(self.step_index, max_upcoming):
      next_step = self.steps[idx]
      next_maneuver = next_step.get("maneuver", {})
      if idx > self.step_index:
        prev_step_distance = max(0.0, _safe_float(self.steps[idx - 1].get("distance", 0.0)))
        cumulative_distance += prev_step_distance
      all_maneuvers.append(
        {
          "distance": cumulative_distance,
          "type": str(next_maneuver.get("type", "")),
          "modifier": str(next_maneuver.get("modifier", "straight")),
        }
      )

    return {
      "maneuverPrimaryText": step.get("name", ""),
      "maneuverSecondaryText": step.get("secondary", ""),
      "maneuverType": maneuver.get("type", ""),
      "maneuverModifier": maneuver.get("modifier", "straight"),
      "maneuverDistance": self.distance_to_maneuver,
      "distanceRemaining": self.distance_remaining,
      "timeRemaining": self.time_remaining,
      "showFull": self.distance_to_maneuver < 120.0,
      "lanes": lanes,
      "allManeuvers": all_maneuvers,
    }

  def is_arrived(self, lat: float, lon: float) -> bool:
    if self.destination is None:
      return False
    return Coordinate(lat, lon).distance_to(self.destination) < ARRIVAL_THRESHOLD_M

  def is_off_route(self, lat: float, lon: float) -> bool:
    if not self.geometry_coords or len(self.geometry_coords) < 2:
      return False
    pos = Coordinate(lat, lon)
    # Check distance to nearest point on geometry
    min_dist = min(pos.distance_to(c) for c in self.geometry_coords)
    return min_dist > OFF_ROUTE_THRESHOLD_M
