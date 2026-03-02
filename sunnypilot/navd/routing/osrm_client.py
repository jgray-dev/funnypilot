from __future__ import annotations

"""
Map routing client.
Prefers Mapbox Directions API (traffic-aware) when tokens are available,
falls back to public OSRM otherwise.
"""

import urllib.request
import urllib.error
import urllib.parse
import json

from openpilot.sunnypilot.navd.mapbox_config import get_mapbox_access_token


OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
MAPBOX_BASE = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic"
TIMEOUT = 10


def _normalize_indication(indication: str) -> str:
  key = (indication or "none").strip().lower().replace("-", "")
  if key in ("slightleft", "left", "right", "straight", "none", "slightright"):
    if key == "slightleft":
      return "slightLeft"
    if key == "slightright":
      return "slightRight"
    return key
  return "none"


def _extract_step_lanes(step: dict) -> list[dict]:
  intersections = step.get("intersections", [])
  for intersection in intersections:
    raw_lanes = intersection.get("lanes")
    if not raw_lanes:
      continue

    lanes: list[dict] = []
    for lane in raw_lanes:
      directions = [_normalize_indication(d) for d in lane.get("indications", [])]
      directions = [d for d in directions if d != "none"]
      if not directions:
        directions = ["none"]

      active_direction = _normalize_indication(lane.get("valid_indication", "none"))
      if active_direction == "none" and lane.get("valid", False):
        active_direction = directions[0]

      lanes.append(
        {
          "active": bool(lane.get("valid", False)),
          "directions": directions,
          "activeDirection": active_direction,
        }
      )

    if lanes:
      return lanes

  return []


def _secondary_text(step: dict) -> str:
  parts: list[str] = []
  destinations = step.get("destinations", "")
  if destinations:
    parts.append(str(destinations))
  exits = step.get("exits", "")
  if exits:
    parts.append(f"Exit {exits}")
  ref = step.get("ref", "")
  if ref:
    parts.append(str(ref))
  return " - ".join(parts)


def _extract_route(data: dict) -> dict | None:
  if data.get("code") != "Ok" or not data.get("routes"):
    return None

  route = data["routes"][0]
  steps = []
  for leg in route.get("legs", []):
    for step in leg.get("steps", []):
      maneuver = step.get("maneuver", {})
      steps.append(
        {
          "name": step.get("name", ""),
          "secondary": _secondary_text(step),
          "distance": step.get("distance", 0.0),
          "duration": step.get("duration", 0.0),
          "lanes": _extract_step_lanes(step),
          "maneuver": {
            "type": maneuver.get("type", ""),
            "modifier": maneuver.get("modifier", "straight"),
            "location": maneuver.get("location", [0.0, 0.0]),
          },
        }
      )

  geometry = route.get("geometry", {})
  total_distance = route.get("distance", 0.0)
  total_duration = route.get("duration", 0.0)

  return {
    "steps": steps,
    "geometry": geometry,
    "distance": total_distance,
    "duration": total_duration,
  }


def _fetch_json(url: str) -> dict | None:
  try:
    req = urllib.request.Request(url, headers={"User-Agent": "FunnyPilot/1.0.2m"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
      return json.loads(resp.read())
  except Exception:
    return None


def _mapbox_url(start_lat: float, start_lon: float, end_lat: float, end_lon: float, token: str) -> str:
  params = {
    "alternatives": "false",
    "geometries": "geojson",
    "overview": "full",
    "steps": "true",
    "banner_instructions": "true",
    "annotations": "duration,distance,speed,congestion",
    "language": "en",
    "access_token": token,
  }
  coords = f"{start_lon},{start_lat};{end_lon},{end_lat}"
  return f"{MAPBOX_BASE}/{coords}?{urllib.parse.urlencode(params)}"


def _osrm_url(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> str:
  return f"{OSRM_BASE}/{start_lon},{start_lat};{end_lon},{end_lat}?steps=true&overview=full&geometries=geojson&annotations=false"


def get_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> dict | None:
  """
  Fetch a driving route from OSRM.
  Returns dict with 'steps' list and 'geometry' (GeoJSON LineString), or None on failure.
  Each step includes maneuver, lane guidance, and optional destination text.
  """
  token = get_mapbox_access_token(prefer_secret=True)
  if token:
    mapbox_data = _fetch_json(_mapbox_url(start_lat, start_lon, end_lat, end_lon, token))
    route = _extract_route(mapbox_data or {})
    if route:
      return route

  osrm_data = _fetch_json(_osrm_url(start_lat, start_lon, end_lat, end_lon))
  return _extract_route(osrm_data or {})
