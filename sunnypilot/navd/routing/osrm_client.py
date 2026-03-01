"""
OSRM public routing API client.
Uses router.project-osrm.org — free, no API key required.
"""
import urllib.request
import urllib.error
import json


OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
TIMEOUT = 10


def get_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> dict | None:
  """
  Fetch a driving route from OSRM.
  Returns dict with 'steps' list and 'geometry' (GeoJSON LineString), or None on failure.
  Each step: {name, distance, duration, maneuver: {type, modifier, location: [lon, lat]}}
  """
  url = (
    f"{OSRM_BASE}/{start_lon},{start_lat};{end_lon},{end_lat}"
    f"?steps=true&overview=full&geometries=geojson&annotations=false"
  )
  try:
    req = urllib.request.Request(url, headers={"User-Agent": "FunnyPilot/0.9.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
      data = json.loads(resp.read())
  except Exception:
    return None

  if data.get("code") != "Ok" or not data.get("routes"):
    return None

  route = data["routes"][0]
  steps = []
  for leg in route.get("legs", []):
    for step in leg.get("steps", []):
      maneuver = step.get("maneuver", {})
      steps.append({
        "name": step.get("name", ""),
        "distance": step.get("distance", 0.0),
        "duration": step.get("duration", 0.0),
        "maneuver": {
          "type": maneuver.get("type", ""),
          "modifier": maneuver.get("modifier", "straight"),
          "location": maneuver.get("location", [0.0, 0.0]),
        },
      })

  geometry = route.get("geometry", {})
  total_distance = route.get("distance", 0.0)
  total_duration = route.get("duration", 0.0)

  return {
    "steps": steps,
    "geometry": geometry,
    "distance": total_distance,
    "duration": total_duration,
  }
