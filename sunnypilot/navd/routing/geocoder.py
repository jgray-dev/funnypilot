"""
Geocoding with Mapbox-first strategy.
Falls back to Photon/Nominatim when no token is configured.
"""

from __future__ import annotations

import urllib.request
import urllib.parse
import json

from openpilot.sunnypilot.navd.mapbox_config import get_mapbox_access_token


PHOTON_BASE = "https://photon.komoot.io/api/"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org/reverse"
MAPBOX_GEOCODE_BASE = "https://api.mapbox.com/geocoding/v5/mapbox.places"
TIMEOUT = 8
HEADERS = {"User-Agent": "FunnyPilot/1.0.2m"}


def _fetch_json(url: str) -> dict | None:
  try:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
      return json.loads(resp.read())
  except Exception:
    return None


def _mapbox_autocomplete(query: str, lat: float | None = None, lon: float | None = None, limit: int = 5) -> list[dict]:
  token = get_mapbox_access_token(prefer_secret=True)
  if not token:
    return []

  params = {
    "access_token": token,
    "autocomplete": "true",
    "language": "en",
    "limit": str(limit),
    "types": "address,poi,place,locality,region",
  }
  if lat is not None and lon is not None:
    params["proximity"] = f"{lon},{lat}"

  safe_query = urllib.parse.quote(query)
  url = f"{MAPBOX_GEOCODE_BASE}/{safe_query}.json?{urllib.parse.urlencode(params)}"
  data = _fetch_json(url)
  if not data:
    return []

  results: list[dict] = []
  for feature in data.get("features", []):
    center = feature.get("center", [None, None])
    if not isinstance(center, list) or len(center) < 2:
      continue
    lon_v, lat_v = center[0], center[1]
    if lat_v is None or lon_v is None:
      continue
    results.append(
      {
        "name": feature.get("text", feature.get("place_name", "Unknown")),
        "address": feature.get("place_name", ""),
        "lat": float(lat_v),
        "lon": float(lon_v),
        "type": feature.get("place_type", [""])[0] if feature.get("place_type") else "",
      }
    )

  return results


def _mapbox_reverse(lat: float, lon: float) -> dict | None:
  token = get_mapbox_access_token(prefer_secret=True)
  if not token:
    return None

  params = {
    "access_token": token,
    "language": "en",
    "limit": "1",
    "types": "address,poi,place,locality,region",
  }
  url = f"{MAPBOX_GEOCODE_BASE}/{lon},{lat}.json?{urllib.parse.urlencode(params)}"
  data = _fetch_json(url)
  if not data:
    return None

  features = data.get("features", [])
  if not features:
    return None
  f = features[0]
  name = f.get("text", f.get("place_name", "Unknown"))
  address = f.get("place_name", "")
  return {"name": name, "address": address}


def autocomplete(query: str, lat: float | None = None, lon: float | None = None, limit: int = 5) -> list[dict]:
  """
  Search for places matching `query`.
  Returns list of {name, address, lat, lon, type}.
  """
  mapbox_results = _mapbox_autocomplete(query, lat, lon, limit)
  if mapbox_results:
    return mapbox_results

  params = {"q": query, "limit": str(limit), "lang": "en"}
  if lat is not None and lon is not None:
    params["lat"] = str(lat)
    params["lon"] = str(lon)

  url = PHOTON_BASE + "?" + urllib.parse.urlencode(params)
  data = _fetch_json(url)
  if not data:
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
    results.append(
      {
        "name": name,
        "address": address,
        "lat": coords[1],
        "lon": coords[0],
        "type": props.get("osm_value", props.get("type", "")),
      }
    )

  return results


def reverse(lat: float, lon: float) -> dict | None:
  """
  Reverse geocode a coordinate.
  Returns {name, address} or None on failure.
  """
  mapbox_reverse = _mapbox_reverse(lat, lon)
  if mapbox_reverse:
    return mapbox_reverse

  params = {"lat": str(lat), "lon": str(lon), "format": "json"}
  url = NOMINATIM_BASE + "?" + urllib.parse.urlencode(params)
  data = _fetch_json(url)
  if not data:
    return None

  display = data.get("display_name", "")
  parts = [p.strip() for p in display.split(",")]
  name = parts[0] if parts else "Unknown"
  address = ", ".join(parts[1:4]) if len(parts) > 1 else display

  return {"name": name, "address": address}
