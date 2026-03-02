#!/usr/bin/env python3

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess
from pathlib import Path


DEFAULT_LOCAL_PATH = Path(".nav_secrets/mapbox_tokens.json")
DEFAULT_DEVICE_PATH = "/data/openpilot/.nav_secrets/mapbox_tokens.json"


def _read_token(cli_value: str | None, env_key: str, prompt: str) -> str:
  if cli_value:
    return cli_value.strip()

  env_val = os.getenv(env_key, "").strip()
  if env_val:
    return env_val

  return getpass.getpass(prompt).strip()


def _write_local(path: Path, payload: dict[str, str]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
  os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _push_remote(ssh_target: str, ssh_proxy_command: str | None, device_path: str, payload: dict[str, str]) -> None:
  remote_dir = str(Path(device_path).parent)
  remote_cmd = f"umask 077 && mkdir -p '{remote_dir}' && cat > '{device_path}' && chmod 600 '{device_path}'"

  cmd = ["ssh"]
  if ssh_proxy_command:
    cmd.extend(["-o", f"ProxyCommand={ssh_proxy_command}"])
  cmd.extend([ssh_target, remote_cmd])

  subprocess.run(cmd, input=json.dumps(payload, indent=2) + "\n", text=True, check=True)


def main() -> None:
  parser = argparse.ArgumentParser(description="Configure Mapbox tokens for FunnyPilot navigation.")
  parser.add_argument("--public-token", help="Mapbox public token (pk.*).")
  parser.add_argument("--secret-token", help="Mapbox secret token (sk.*).")
  parser.add_argument("--local-path", default=str(DEFAULT_LOCAL_PATH), help="Local token file path.")
  parser.add_argument("--ssh-target", help="Optional SSH target to install tokens on device (e.g. comma@100.93.118.118).")
  parser.add_argument("--ssh-proxy-command", help="Optional SSH ProxyCommand string for remote installs.")
  parser.add_argument("--device-path", default=DEFAULT_DEVICE_PATH, help="Remote token file path on device.")
  args = parser.parse_args()

  public_token = _read_token(args.public_token, "MAPBOX_PUBLIC_TOKEN", "Mapbox public token (pk.*): ")
  secret_token = _read_token(args.secret_token, "MAPBOX_SECRET_TOKEN", "Mapbox secret token (sk.*): ")

  if not public_token and not secret_token:
    raise SystemExit("No token provided.")

  payload = {
    "public_token": public_token,
    "secret_token": secret_token,
  }

  local_path = Path(args.local_path)
  _write_local(local_path, payload)
  print(f"Wrote local tokens to: {local_path}")

  if args.ssh_target:
    _push_remote(args.ssh_target, args.ssh_proxy_command, args.device_path, payload)
    print(f"Installed tokens on device: {args.ssh_target}:{args.device_path}")


if __name__ == "__main__":
  main()
