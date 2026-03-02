from __future__ import annotations

import json
import os
from pathlib import Path


_DEFAULT_SECRET_PATHS = [
  "/data/openpilot/.nav_secrets/mapbox_tokens.json",
  "/data/params/d/.nav_secrets/mapbox_tokens.json",
  ".nav_secrets/mapbox_tokens.json",
]


def _read_json_file(path: Path) -> dict | None:
  try:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else None
  except Exception:
    return None


def load_mapbox_tokens() -> dict[str, str]:
  env_public = os.getenv("MAPBOX_PUBLIC_TOKEN", "").strip()
  env_secret = os.getenv("MAPBOX_SECRET_TOKEN", "").strip()
  if env_public or env_secret:
    return {
      "public_token": env_public,
      "secret_token": env_secret,
    }

  config_path = os.getenv("FUNNYPILOT_MAPBOX_CONFIG", "").strip()
  candidate_paths = [config_path] if config_path else []
  candidate_paths.extend(_DEFAULT_SECRET_PATHS)

  for p in candidate_paths:
    if not p:
      continue
    path = Path(p)
    if not path.is_absolute():
      path = Path(__file__).resolve().parents[2] / p
    if not path.exists():
      continue
    data = _read_json_file(path)
    if not data:
      continue

    public_token = str(data.get("public_token", "")).strip()
    secret_token = str(data.get("secret_token", "")).strip()
    if public_token or secret_token:
      return {
        "public_token": public_token,
        "secret_token": secret_token,
      }

  return {
    "public_token": "",
    "secret_token": "",
  }


def get_mapbox_access_token(prefer_secret: bool = True) -> str:
  tokens = load_mapbox_tokens()
  if prefer_secret:
    return tokens.get("secret_token", "") or tokens.get("public_token", "")
  return tokens.get("public_token", "") or tokens.get("secret_token", "")
