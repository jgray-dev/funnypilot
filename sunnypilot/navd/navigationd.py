"""
FunnyPilot navigationd — 3 Hz navigation daemon.

Watches NavDestination param, fetches OSRM routes, tracks route progress,
and publishes navInstruction + navigationStateSP via cereal.
"""

from __future__ import annotations

import json
import time

import cereal.messaging as messaging
from cereal import log
from openpilot.common.params import Params, UnknownKeyName
from openpilot.sunnypilot.navd.nav_state import NavState
from openpilot.sunnypilot.navd.routing.osrm_client import get_route
from openpilot.sunnypilot.navd.routing.route_cache import RouteCache, BreadcrumbTracker, build_rejoin_route

LOOP_HZ = 3
REROUTE_COOLDOWN_S = 10.0


_NAV_DIRECTION_MAP = {
  "none": log.NavInstruction.Direction.none,
  "left": log.NavInstruction.Direction.left,
  "right": log.NavInstruction.Direction.right,
  "straight": log.NavInstruction.Direction.straight,
  "slightleft": log.NavInstruction.Direction.slightLeft,
  "slightright": log.NavInstruction.Direction.slightRight,
}


def _nav_direction_from_text(direction: str) -> int:
  norm = (direction or "none").strip().lower().replace("-", "")
  return _NAV_DIRECTION_MAP.get(norm, log.NavInstruction.Direction.none)


def _set_nav_lanes(ni, lanes: list[dict]) -> None:
  lane_list = ni.init("lanes", len(lanes))
  for i, lane in enumerate(lanes):
    lane_msg = lane_list[i]
    lane_msg.active = bool(lane.get("active", False))

    directions = lane.get("directions", [])
    if not isinstance(directions, list):
      directions = []
    direction_list = lane_msg.init("directions", len(directions))
    for j, direction in enumerate(directions):
      direction_list[j] = _nav_direction_from_text(str(direction))

    active_direction = lane.get("activeDirection", "none")
    lane_msg.activeDirection = _nav_direction_from_text(str(active_direction))


def _set_nav_maneuvers(ni, maneuvers: list[dict]) -> None:
  maneuver_list = ni.init("allManeuvers", len(maneuvers))
  for i, maneuver in enumerate(maneuvers):
    maneuver_msg = maneuver_list[i]
    maneuver_msg.distance = float(max(0.0, maneuver.get("distance", 0.0)))
    maneuver_msg.type = str(maneuver.get("type", ""))
    maneuver_msg.modifier = str(maneuver.get("modifier", "straight"))


def _load_destination(dest_str: str | None) -> dict | None:
  if not dest_str:
    return None
  try:
    dest = json.loads(dest_str)
    lat = dest.get("lat", dest.get("latitude"))
    lon = dest.get("lon", dest.get("longitude"))
    if lat is None or lon is None:
      return None
    return {
      "lat": float(lat),
      "lon": float(lon),
      "name": str(dest.get("name", dest.get("place_name", ""))),
      "address": str(dest.get("address", dest.get("place_details", ""))),
    }
  except Exception:
    return None


def main():
  params = Params()
  pm = messaging.PubMaster(["navInstruction", "navigationStateSP"])
  sm = messaging.SubMaster(["gpsLocationExternal"])

  nav_state = NavState()
  route_cache = RouteCache()
  breadcrumb_tracker = BreadcrumbTracker()
  last_dest_json: str | None = None
  last_reroute_time: float = 0.0
  last_gps_lat: float = 0.0
  last_gps_lon: float = 0.0
  gps_valid: bool = False
  nav_destination_supported = True

  try:
    params.check_key("NavDestination")
  except UnknownKeyName:
    nav_destination_supported = False

  def fetch_online_route(start_lat: float, start_lon: float, dest: dict) -> dict | None:
    route = get_route(start_lat, start_lon, dest["lat"], dest["lon"])
    if route:
      route_cache.save_route(start_lat, start_lon, dest["lat"], dest["lon"], dest["name"], dest["address"], route)
    return route

  def fetch_route_with_cache(start_lat: float, start_lon: float, dest: dict) -> tuple[dict | None, str]:
    route = fetch_online_route(start_lat, start_lon, dest)
    if route:
      return route, "online"

    cached = route_cache.load_best_route(start_lat, start_lon, dest["lat"], dest["lon"])
    if cached:
      return cached, "cache"

    return None, "none"

  while True:
    t_start = time.monotonic()

    # --- Poll GPS ---
    sm.update(0)
    gps = sm["gpsLocationExternal"]
    if gps.horizontalAccuracy < 50.0 and (gps.latitude != 0.0 or gps.longitude != 0.0):
      last_gps_lat = gps.latitude
      last_gps_lon = gps.longitude
      gps_valid = True

    # --- Check for new/cleared destination ---
    dest_str = None
    if nav_destination_supported:
      try:
        dest_json = params.get("NavDestination")
        dest_str = dest_json.decode() if isinstance(dest_json, bytes) else dest_json
      except UnknownKeyName:
        nav_destination_supported = False
      except Exception:
        dest_str = None

    if dest_str != last_dest_json:
      last_dest_json = dest_str
      nav_state.clear()
      breadcrumb_tracker.clear()

      if dest_str:
        dest = _load_destination(dest_str)
        if dest is not None and gps_valid:
          route, _ = fetch_route_with_cache(last_gps_lat, last_gps_lon, dest)
          if route:
            nav_state.set_route(route, dest["lat"], dest["lon"], dest["name"], dest["address"])

    # Destination can be set before GPS lock. Once GPS is valid, fetch route.
    elif dest_str and gps_valid and not nav_state.active:
      dest = _load_destination(dest_str)
      if dest is not None:
        route, _ = fetch_route_with_cache(last_gps_lat, last_gps_lon, dest)
        if route:
          nav_state.set_route(route, dest["lat"], dest["lon"], dest["name"], dest["address"])

    # --- Update progress ---
    if nav_state.active and gps_valid:
      breadcrumb_tracker.add(last_gps_lat, last_gps_lon)
      nav_state.update(last_gps_lat, last_gps_lon)

      # Arrival check
      if nav_state.is_arrived(last_gps_lat, last_gps_lon):
        if nav_destination_supported:
          try:
            params.remove("NavDestination")
          except UnknownKeyName:
            nav_destination_supported = False
        nav_state.clear()
        last_dest_json = None

      # Off-route re-route
      elif nav_state.is_off_route(last_gps_lat, last_gps_lon):
        now = time.monotonic()
        if now - last_reroute_time > REROUTE_COOLDOWN_S and dest_str:
          last_reroute_time = now
          dest = _load_destination(dest_str)
          if dest is not None:
            route = fetch_online_route(last_gps_lat, last_gps_lon, dest)
            if route:
              nav_state.set_route(route, dest["lat"], dest["lon"], dest["name"], dest["address"])
              breadcrumb_tracker.clear()
            else:
              recovery = build_rejoin_route(last_gps_lat, last_gps_lon, breadcrumb_tracker.points(), nav_state.geometry_coords)
              if recovery:
                nav_state.set_route(recovery, dest["lat"], dest["lon"], dest["name"], dest["address"])
              else:
                cached = route_cache.load_best_route(last_gps_lat, last_gps_lon, dest["lat"], dest["lon"])
                if cached:
                  nav_state.set_route(cached, dest["lat"], dest["lon"], dest["name"], dest["address"])

    # --- Publish navInstruction ---
    nav_msg = messaging.new_message("navInstruction")
    ni = nav_msg.navInstruction
    if nav_state.active:
      instr = nav_state.current_instruction()
      ni.maneuverPrimaryText = instr.get("maneuverPrimaryText", "")
      ni.maneuverSecondaryText = instr.get("maneuverSecondaryText", "")
      ni.maneuverType = instr.get("maneuverType", "")
      ni.maneuverModifier = instr.get("maneuverModifier", "straight")
      ni.maneuverDistance = instr.get("maneuverDistance", 0.0)
      ni.distanceRemaining = instr.get("distanceRemaining", 0.0)
      ni.timeRemaining = instr.get("timeRemaining", 0.0)
      ni.showFull = bool(instr.get("showFull", False))

      try:
        _set_nav_lanes(ni, instr.get("lanes", []))
      except Exception:
        pass

      try:
        _set_nav_maneuvers(ni, instr.get("allManeuvers", []))
      except Exception:
        pass
    pm.send("navInstruction", nav_msg)

    # --- Publish navigationStateSP ---
    state_msg = messaging.new_message("navigationStateSP")
    ns = state_msg.navigationStateSP
    ns.active = nav_state.active
    if nav_state.active:
      ns.destinationName = nav_state.dest_name
      ns.destinationAddr = nav_state.dest_addr
      ns.distanceRemaining = nav_state.distance_remaining
      ns.timeRemaining = nav_state.time_remaining
    pm.send("navigationStateSP", state_msg)

    # --- Maintain loop rate ---
    elapsed = time.monotonic() - t_start
    sleep_time = max(0.0, (1.0 / LOOP_HZ) - elapsed)
    time.sleep(sleep_time)


if __name__ == "__main__":
  main()
