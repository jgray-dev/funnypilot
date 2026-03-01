"""
Geocoding via Photon (Komoot) — free, OSM-backed, no API key.
Reverse geocoding via Nominatim.
"""
import urllib.request
import urllib.parse
import json


PHOTON_BASE = "https://photon.komoot.io/api/"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org/reverse"
TIMEOUT = 8
HEADERS = {"User-Agent": "FunnyPilot/0.9.9"}


def autocomplete(query: str, lat: float | None = None, lon: float | None = None, limit: int = 5) -> list[dict]:
  """
  Search for places matching `query`.
  Returns list of {name, address, lat, lon, type}.
  """
  params = {"q": query, "limit": str(limit), "lang": "en"}
  if lat is not None and lon is not None:
    params["lat"] = str(lat)
    params["lon"] = str(lon)

  url = PHOTON_BASE + "?" + urllib.parse.urlencode(params)
  try:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
      data = json.loads(resp.read())
  except Exception:
    return []

  results = []
  for feature in data.get("features", []):
    props = feature.get("properties", {})
    coords = feature.get("geometry", {}).get("coordinates", [0, 0])
    name = props.get("name", props.get("street", "Unknown"))
    city = props.get("city", props.get("town", props.get("village", "")))
    state = props.get("state", "")
    country = props.get("country", "")
    addr_parts = [p for p in [city, state, country] if p]
    address = ", ".join(addr_parts) if addr_parts else props.get("country", "")
    results.append({
      "name": name,
      "address": address,
      "lat": coords[1],
      "lon": coords[0],
      "type": props.get("osm_value", props.get("type", "")),
    })

  return results


def reverse(lat: float, lon: float) -> dict | None:
  """
  Reverse geocode a coordinate.
  Returns {name, address} or None on failure.
  """
  params = {"lat": str(lat), "lon": str(lon), "format": "json"}
  url = NOMINATIM_BASE + "?" + urllib.parse.urlencode(params)
  try:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
      data = json.loads(resp.read())
  except Exception:
    return None

  display = data.get("display_name", "")
  parts = [p.strip() for p in display.split(",")]
  name = parts[0] if parts else "Unknown"
  address = ", ".join(parts[1:4]) if len(parts) > 1 else display

  return {"name": name, "address": address}
