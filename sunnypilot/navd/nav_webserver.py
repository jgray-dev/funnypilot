"""
FunnyPilot nav_webserver — aiohttp REST server on port 8888.

Provides destination management API + web UI for navigation.
Proxies geocoding/routing calls so browser can stay tokenless.
"""

from __future__ import annotations

import asyncio
import json
import math
import os

import cereal.messaging as messaging
from aiohttp import web

from openpilot.common.params import Params
from openpilot.sunnypilot.navd.mapbox_config import load_mapbox_tokens
from openpilot.sunnypilot.navd.nav_params import get_raw as nav_get_raw
from openpilot.sunnypilot.navd.nav_params import put_json as nav_put_json
from openpilot.sunnypilot.navd.nav_params import remove as nav_remove
from openpilot.sunnypilot.navd.routing.osrm_client import get_route
from openpilot.sunnypilot.navd.routing.geocoder import autocomplete as geocode_autocomplete

PORT = 8888
WEB_DIR = os.path.join(os.path.dirname(__file__), "nav_web")


try:
  _GPS_SM = messaging.SubMaster(["gpsLocationExternal"])
except Exception:
  _GPS_SM = None


def _params_get_json(params: Params, key: str) -> dict | None:
  val = nav_get_raw(params, key)
  if val is None:
    return None
  try:
    s = val.decode() if isinstance(val, bytes) else val
    return json.loads(s)
  except Exception:
    return None


def _params_put_json(params: Params, key: str, data: dict) -> bool:
  return nav_put_json(params, key, data)


def _gps_from_params(params: Params) -> dict | None:
  for key in ("LastGPSPosition", "LastGPSPositionLLK"):
    data = _params_get_json(params, key)
    if not isinstance(data, dict):
      continue
    lat = data.get("latitude", data.get("lat"))
    lon = data.get("longitude", data.get("lon"))
    if lat is None or lon is None:
      continue
    try:
      lat_f = float(lat)
      lon_f = float(lon)
    except Exception:
      continue
    if lat_f != 0.0 or lon_f != 0.0:
      data["latitude"] = lat_f
      data["longitude"] = lon_f
      return data
  return None


def _gps_live() -> dict | None:
  if _GPS_SM is None:
    return None
  try:
    _GPS_SM.update(0)
    gps = _GPS_SM["gpsLocationExternal"]
    lat = float(gps.latitude)
    lon = float(gps.longitude)
    if (lat == 0.0 and lon == 0.0) or float(gps.horizontalAccuracy) >= 100.0:
      return None
    return {
      "latitude": lat,
      "longitude": lon,
      "accuracy": float(gps.horizontalAccuracy),
      "speed": float(gps.speed),
      "bearingDeg": float(gps.bearingDeg),
    }
  except Exception:
    return None


def _gps_best(params: Params) -> dict | None:
  return _gps_from_params(params) or _gps_live()


async def index(request):
  return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def api_status(request):
  params = Params()
  dest = _params_get_json(params, "NavDestination")
  gps = _gps_best(params)
  dest_lat = dest.get("lat", dest.get("latitude", 0.0)) if dest else 0.0
  dest_lon = dest.get("lon", dest.get("longitude", 0.0)) if dest else 0.0
  dest_name = dest.get("name", dest.get("place_name", "")) if dest else ""
  dest_addr = dest.get("address", dest.get("place_details", "")) if dest else ""
  result = {
    "active": dest is not None,
    "dest_name": dest_name,
    "dest_addr": dest_addr,
    "dest_lat": dest_lat,
    "dest_lon": dest_lon,
    "gps_lat": gps.get("latitude", 0.0) if gps else 0.0,
    "gps_lon": gps.get("longitude", 0.0) if gps else 0.0,
  }
  return web.json_response(result)


async def api_config(request):
  tokens = load_mapbox_tokens()
  return web.json_response(
    {
      "mapboxPublicToken": tokens.get("public_token", ""),
      "mapboxConfigured": bool(tokens.get("public_token") or tokens.get("secret_token")),
    }
  )


async def api_set_destination(request):
  try:
    body = await request.json()
    lat_raw = body.get("lat", body.get("latitude"))
    lon_raw = body.get("lon", body.get("longitude"))
    if lat_raw is None or lon_raw is None:
      return web.json_response({"error": "Missing lat/lon"}, status=400)

    lat = float(lat_raw)
    lon = float(lon_raw)
    if not (math.isfinite(lat) and math.isfinite(lon)):
      return web.json_response({"error": "Invalid lat/lon"}, status=400)

    dest = {
      "lat": lat,
      "lon": lon,
      "name": str(body.get("name", "")),
      "address": str(body.get("address", "")),
    }
    if not _params_put_json(Params(), "NavDestination", dest):
      return web.json_response({"error": "Nav destination param unavailable"}, status=503)
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=400)


async def api_clear_destination(request):
  if not nav_remove(Params(), "NavDestination"):
    return web.json_response({"error": "Nav destination param unavailable"}, status=503)
  return web.json_response({"ok": True})


async def api_get_home(request):
  data = _params_get_json(Params(), "NavHomeLocation")
  return web.json_response(data or {})


async def api_set_home(request):
  try:
    body = await request.json()
    if not _params_put_json(Params(), "NavHomeLocation", body):
      return web.json_response({"error": "Home location param unavailable"}, status=503)
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=400)


async def api_get_work(request):
  data = _params_get_json(Params(), "NavWorkLocation")
  return web.json_response(data or {})


async def api_set_work(request):
  try:
    body = await request.json()
    if not _params_put_json(Params(), "NavWorkLocation", body):
      return web.json_response({"error": "Work location param unavailable"}, status=503)
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=400)


async def api_autocomplete(request):
  q = request.rel_url.query.get("q", "").strip()
  if not q:
    return web.json_response([])
  try:
    lat_s = request.rel_url.query.get("lat")
    lon_s = request.rel_url.query.get("lon")
    lat = float(lat_s) if lat_s else None
    lon = float(lon_s) if lon_s else None
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, lambda: geocode_autocomplete(q, lat, lon))
    return web.json_response(results)
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def api_gps(request):
  data = _gps_best(Params())
  return web.json_response(data or {})


async def api_route_preview(request):
  try:
    q = request.rel_url.query
    start_lat = float(q.get("start_lat", ""))
    start_lon = float(q.get("start_lon", ""))
    end_lat = float(q.get("end_lat", ""))
    end_lon = float(q.get("end_lon", ""))
  except Exception:
    return web.json_response({"error": "Invalid route coordinates"}, status=400)

  route = await asyncio.get_event_loop().run_in_executor(None, lambda: get_route(start_lat, start_lon, end_lat, end_lon))
  if route is None:
    return web.json_response({"error": "Route unavailable"}, status=503)

  return web.json_response(
    {
      "distance": route.get("distance", 0.0),
      "duration": route.get("duration", 0.0),
      "geometry": route.get("geometry", {"type": "LineString", "coordinates": []}),
    }
  )


def main():
  app = web.Application()
  app.router.add_get("/", index)
  app.router.add_get("/index.html", index)
  app.router.add_static("/static", WEB_DIR)
  app.router.add_get("/api/status", api_status)
  app.router.add_get("/api/config", api_config)
  app.router.add_post("/api/destination", api_set_destination)
  app.router.add_delete("/api/destination", api_clear_destination)
  app.router.add_get("/api/home", api_get_home)
  app.router.add_post("/api/home", api_set_home)
  app.router.add_get("/api/work", api_get_work)
  app.router.add_post("/api/work", api_set_work)
  app.router.add_get("/api/autocomplete", api_autocomplete)
  app.router.add_get("/api/gps", api_gps)
  app.router.add_get("/api/route_preview", api_route_preview)

  # reuse_address prevents TIME_WAIT bind failures on rapid restart
  web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, reuse_address=True, reuse_port=False)


if __name__ == "__main__":
  main()
