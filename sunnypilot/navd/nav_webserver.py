"""
FunnyPilot nav_webserver — aiohttp REST server on port 8888.

Provides destination management API + web UI for navigation.
Proxies geocoding calls to Photon/Nominatim so browser avoids CORS issues.
"""

import asyncio
import json
import os
import urllib.request
import urllib.parse

from aiohttp import web

from openpilot.common.params import Params
from openpilot.sunnypilot.navd.routing.geocoder import autocomplete as geocode_autocomplete

PORT = 8888
WEB_DIR = os.path.join(os.path.dirname(__file__), "nav_web")


def _params_get_json(params: Params, key: str) -> dict | None:
  val = params.get(key)
  if val is None:
    return None
  try:
    s = val.decode() if isinstance(val, bytes) else val
    return json.loads(s)
  except Exception:
    return None


def _params_put_json(params: Params, key: str, data: dict) -> None:
  params.put(key, json.dumps(data))


async def index(request):
  return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def api_status(request):
  params = Params()
  dest = _params_get_json(params, "NavDestination")
  gps = _params_get_json(params, "LastGPSPosition")
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


async def api_set_destination(request):
  try:
    body = await request.json()
    required = ("lat", "lon")
    if not all(k in body for k in required):
      return web.json_response({"error": "Missing lat/lon"}, status=400)
    dest = {
      "lat": float(body["lat"]),
      "lon": float(body["lon"]),
      "name": str(body.get("name", "")),
      "address": str(body.get("address", "")),
    }
    Params().put("NavDestination", json.dumps(dest))
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=400)


async def api_clear_destination(request):
  Params().remove("NavDestination")
  return web.json_response({"ok": True})


async def api_get_home(request):
  data = _params_get_json(Params(), "NavHomeLocation")
  return web.json_response(data or {})


async def api_set_home(request):
  try:
    body = await request.json()
    _params_put_json(Params(), "NavHomeLocation", body)
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=400)


async def api_get_work(request):
  data = _params_get_json(Params(), "NavWorkLocation")
  return web.json_response(data or {})


async def api_set_work(request):
  try:
    body = await request.json()
    _params_put_json(Params(), "NavWorkLocation", body)
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
  data = _params_get_json(Params(), "LastGPSPosition")
  return web.json_response(data or {})


def main():
  app = web.Application()
  app.router.add_get("/", index)
  app.router.add_get("/index.html", index)
  app.router.add_static("/static", WEB_DIR)
  app.router.add_get("/api/status", api_status)
  app.router.add_post("/api/destination", api_set_destination)
  app.router.add_delete("/api/destination", api_clear_destination)
  app.router.add_get("/api/home", api_get_home)
  app.router.add_post("/api/home", api_set_home)
  app.router.add_get("/api/work", api_get_work)
  app.router.add_post("/api/work", api_set_work)
  app.router.add_get("/api/autocomplete", api_autocomplete)
  app.router.add_get("/api/gps", api_gps)

  web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
  main()
