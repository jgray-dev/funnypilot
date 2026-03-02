from __future__ import annotations

import json
import os

from openpilot.common.params import Params, UnknownKeyName

_FALLBACK_DIR = "/data/params/funnypilot_nav"
_KEY_TO_FILE = {
  "NavDestination": "destination.json",
  "NavHomeLocation": "home.json",
  "NavWorkLocation": "work.json",
}


def _fallback_path(key: str) -> str | None:
  name = _KEY_TO_FILE.get(key)
  if not name:
    return None
  return os.path.join(_FALLBACK_DIR, name)


def _fallback_get(key: str) -> bytes | None:
  path = _fallback_path(key)
  if path is None or not os.path.isfile(path):
    return None
  try:
    with open(path, "rb") as f:
      data = f.read()
    return data if data else None
  except Exception:
    return None


def _fallback_put_json(key: str, data: dict) -> bool:
  path = _fallback_path(key)
  if path is None:
    return False
  try:
    os.makedirs(_FALLBACK_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)
    return True
  except Exception:
    return False


def _fallback_remove(key: str) -> bool:
  path = _fallback_path(key)
  if path is None:
    return False
  try:
    if os.path.exists(path):
      os.remove(path)
    return True
  except Exception:
    return False


def key_available(params: Params, key: str) -> bool:
  try:
    return bool(params.check_key(key))
  except UnknownKeyName:
    return key in _KEY_TO_FILE
  except Exception:
    return key in _KEY_TO_FILE


def get_raw(params: Params, key: str) -> bytes | None:
  try:
    return params.get(key)
  except UnknownKeyName:
    return _fallback_get(key)
  except Exception:
    return _fallback_get(key)


def put_json(params: Params, key: str, data: dict) -> bool:
  try:
    params.put(key, json.dumps(data))
    return True
  except UnknownKeyName:
    return _fallback_put_json(key, data)
  except Exception:
    return _fallback_put_json(key, data)


def remove(params: Params, key: str) -> bool:
  try:
    params.remove(key)
    return True
  except UnknownKeyName:
    return _fallback_remove(key)
  except Exception:
    return _fallback_remove(key)
