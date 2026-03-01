"""
Disk-backed route cache and offline route recovery helpers.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from collections import deque

from openpilot.sunnypilot.navd.helpers import Coordinate

MAX_ROUTES = 40
MAX_CACHE_BYTES = 35 * 1024 * 1024
DEST_MATCH_M = 80.0
START_MATCH_M = 6000.0
RECOVERY_MAX_POINTS = 120


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  return Coordinate(lat1, lon1).distance_to(Coordinate(lat2, lon2))


def _route_id(start_lat: float, start_lon: float, dest_lat: float, dest_lon: float, created_at: float) -> str:
  seed = f"{start_lat:.6f}:{start_lon:.6f}:{dest_lat:.6f}:{dest_lon:.6f}:{created_at:.3f}"
  return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _route_distance(coords: list[Coordinate]) -> float:
  if len(coords) < 2:
    return 0.0
  total = 0.0
  for i in range(len(coords) - 1):
    total += coords[i].distance_to(coords[i + 1])
  return total


def _estimate_duration(distance_m: float) -> float:
  # Conservative city/highway blend.
  return distance_m / 12.5 if distance_m > 0 else 0.0


def _modifier_from_path(a: Coordinate, b: Coordinate, c: Coordinate) -> str:
  import math

  def bearing(p1: Coordinate, p2: Coordinate) -> float:
    y = math.sin(math.radians(p2.longitude - p1.longitude)) * math.cos(math.radians(p2.latitude))
    x = math.cos(math.radians(p1.latitude)) * math.sin(math.radians(p2.latitude)) - math.sin(math.radians(p1.latitude)) * math.cos(
      math.radians(p2.latitude)
    ) * math.cos(math.radians(p2.longitude - p1.longitude))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

  b1 = bearing(a, b)
  b2 = bearing(b, c)
  delta = (b2 - b1 + 540.0) % 360.0 - 180.0
  if delta > 25.0:
    return "right"
  if delta < -25.0:
    return "left"
  return "straight"


def _build_steps_from_geometry(coords: list[Coordinate]) -> list[dict]:
  if len(coords) < 2:
    return []

  steps = []
  accum = 0.0
  next_mark = 80.0
  idx = 1

  while idx < len(coords):
    accum += coords[idx - 1].distance_to(coords[idx])
    if accum >= next_mark or idx == len(coords) - 1:
      modifier = "straight"
      if idx >= 2:
        modifier = _modifier_from_path(coords[idx - 2], coords[idx - 1], coords[idx])

      steps.append(
        {
          "name": "Backtrack" if len(steps) == 0 else "Continue",
          "distance": accum,
          "duration": _estimate_duration(accum),
          "maneuver": {
            "type": "turn",
            "modifier": modifier,
            "location": [coords[idx].longitude, coords[idx].latitude],
          },
        }
      )
      next_mark += 120.0
      if len(steps) >= 20:
        break
    idx += 1

  return steps


def _nearest_geometry_index(pos: Coordinate, geometry: list[Coordinate]) -> int:
  best_i = 0
  best_d = 1e9
  for i, c in enumerate(geometry):
    d = pos.distance_to(c)
    if d < best_d:
      best_i = i
      best_d = d
  return best_i


class RouteCache:
  def __init__(self, cache_dir: str | None = None):
    if cache_dir is None:
      primary = "/data/openpilot/.funnypilot_nav_cache"
      fallback = "/tmp/funnypilot_nav_cache"
      cache_dir = primary if os.path.isdir("/data/openpilot") else fallback

    self.cache_dir = cache_dir
    self.routes_dir = os.path.join(cache_dir, "routes")
    self.index_path = os.path.join(cache_dir, "index.json")
    self._ensure_dirs()

  def _ensure_dirs(self) -> None:
    os.makedirs(self.routes_dir, exist_ok=True)

  def _read_index(self) -> list[dict]:
    try:
      with open(self.index_path, "r", encoding="utf-8") as f:
        idx = json.load(f)
      if isinstance(idx, list):
        return idx
    except Exception:
      pass
    return []

  def _write_index(self, entries: list[dict]) -> None:
    tmp_path = f"{self.index_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(entries, f, ensure_ascii=True)
    os.replace(tmp_path, self.index_path)

  def _prune(self, entries: list[dict]) -> list[dict]:
    entries = sorted(entries, key=lambda e: (e.get("last_used", 0.0), e.get("created_at", 0.0)), reverse=True)
    kept = []
    total = 0
    for e in entries:
      sz = int(e.get("size", 0))
      if len(kept) >= MAX_ROUTES or total + sz > MAX_CACHE_BYTES:
        try:
          os.remove(e.get("file", ""))
        except Exception:
          pass
        continue
      if os.path.exists(e.get("file", "")):
        kept.append(e)
        total += sz
    return kept

  def save_route(self, start_lat: float, start_lon: float, dest_lat: float, dest_lon: float, dest_name: str, dest_addr: str, route: dict) -> None:
    try:
      created = time.time()
      rid = _route_id(start_lat, start_lon, dest_lat, dest_lon, created)
      route_path = os.path.join(self.routes_dir, f"{rid}.json.gz")

      payload = {
        "route": route,
      }
      with gzip.open(route_path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True)

      size = os.path.getsize(route_path)
      entries = self._read_index()
      entries.append(
        {
          "id": rid,
          "created_at": created,
          "last_used": created,
          "use_count": 1,
          "file": route_path,
          "size": size,
          "start_lat": start_lat,
          "start_lon": start_lon,
          "dest_lat": dest_lat,
          "dest_lon": dest_lon,
          "dest_name": dest_name,
          "dest_addr": dest_addr,
        }
      )

      entries = self._prune(entries)
      self._write_index(entries)
    except Exception:
      return

  def load_best_route(self, start_lat: float, start_lon: float, dest_lat: float, dest_lon: float) -> dict | None:
    entries = self._read_index()
    if not entries:
      return None

    candidates = []
    for e in entries:
      d_dest = _haversine_m(dest_lat, dest_lon, float(e.get("dest_lat", 0.0)), float(e.get("dest_lon", 0.0)))
      if d_dest > DEST_MATCH_M:
        continue
      d_start = _haversine_m(start_lat, start_lon, float(e.get("start_lat", 0.0)), float(e.get("start_lon", 0.0)))
      if d_start > START_MATCH_M:
        continue
      score = d_dest * 10.0 + d_start - min(float(e.get("use_count", 0)) * 20.0, 200.0)
      candidates.append((score, e))

    if not candidates:
      return None

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]

    try:
      with gzip.open(chosen["file"], "rt", encoding="utf-8") as f:
        payload = json.load(f)
      route = payload.get("route")
      if not isinstance(route, dict):
        return None

      chosen["last_used"] = time.time()
      chosen["use_count"] = int(chosen.get("use_count", 0)) + 1
      updated = [chosen if e.get("id") == chosen.get("id") else e for e in entries]
      self._write_index(updated)
      return route
    except Exception:
      return None


class BreadcrumbTracker:
  def __init__(self):
    self._points: deque[Coordinate] = deque(maxlen=900)

  def clear(self) -> None:
    self._points.clear()

  def add(self, lat: float, lon: float) -> None:
    p = Coordinate(lat, lon)
    if not self._points:
      self._points.append(p)
      return
    if self._points[-1].distance_to(p) >= 8.0:
      self._points.append(p)

  def points(self) -> list[Coordinate]:
    return list(self._points)


def build_rejoin_route(current_lat: float, current_lon: float, breadcrumbs: list[Coordinate], original_geometry: list[Coordinate]) -> dict | None:
  if len(original_geometry) < 2:
    return None

  current = Coordinate(current_lat, current_lon)
  rejoin_idx = _nearest_geometry_index(current, original_geometry)

  backtrack = []
  for p in reversed(breadcrumbs):
    if backtrack and backtrack[-1].distance_to(p) < 8.0:
      continue
    backtrack.append(p)
    if p.distance_to(original_geometry[rejoin_idx]) < 30.0 or len(backtrack) >= RECOVERY_MAX_POINTS:
      break

  if backtrack:
    rejoin_idx = _nearest_geometry_index(backtrack[-1], original_geometry)

  composed = [current]
  composed.extend(backtrack)
  composed.extend(original_geometry[rejoin_idx:])

  deduped = [composed[0]] if composed else []
  for p in composed[1:]:
    if deduped[-1].distance_to(p) >= 3.0:
      deduped.append(p)

  if len(deduped) < 2:
    return None

  steps = _build_steps_from_geometry(deduped)
  distance = _route_distance(deduped)

  return {
    "steps": steps,
    "geometry": {
      "type": "LineString",
      "coordinates": [[p.longitude, p.latitude] for p in deduped],
    },
    "distance": distance,
    "duration": _estimate_duration(distance),
  }
