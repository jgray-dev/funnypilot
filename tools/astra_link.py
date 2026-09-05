#!/usr/bin/env python3
"""Local Astra pairing/configuration; credentials are prompted, never arguments."""

import argparse
import getpass
import os
import re
import secrets
import sys
import threading
import time

if __name__ == "__main__":
  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sunnypilot.astra_link.core import CONFIG, Transport, load_config, private_write
from sunnypilot.astra_link.state import Safety


def configure(action, safety, transport_factory=Transport, path=CONFIG, prompt=getpass.getpass):
  if not safety.offroad():
    raise ValueError("fresh offroad state required")
  try:
    config = load_config(path)
  except FileNotFoundError:
    config = {"enabled": False, "token": secrets.token_hex(32)}
  if action == "pair":
    if config.get("deviceId"):
      raise ValueError("already paired; revoke remotely before replacing local credentials")
    secret = prompt("Pairing secret (hidden): ")
    if not re.fullmatch(r"[a-f0-9]{64}", secret):
      raise ValueError("invalid pairing secret")
    if not safety.offroad():
      raise ValueError("offroad permission expired")
    # Persist first. Retrying a lost exchange reuses exactly the same token.
    private_write(path, config)
    reply = transport_factory().post("pair", {"pairingSecret": secret, "token": config["token"]})
    device_id = reply.get("deviceId")
    if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device_id):
      raise ValueError("invalid pairing response")
    config.update(deviceId=device_id, enabled=True)
  else:
    if action == "enable" and not config.get("deviceId"):
      raise ValueError("pair device first")
    config["enabled"] = action == "enable"
  if not safety.offroad():
    raise ValueError("offroad permission expired; retry locally")
  private_write(path, config)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("action", choices=("pair", "enable", "disable", "status"))
  args = parser.parse_args()
  try:
    if args.action == "status":
      config = load_config()
      print("paired=" + str(bool(config.get("deviceId"))) + " enabled=" + str(config["enabled"]))
      return 0
    if args.action == "pair" and not sys.stdin.isatty():
      raise ValueError("pairing requires an interactive terminal")
    stop = threading.Event()
    safety = Safety()
    safety.start(stop)
    try:
      deadline = time.monotonic() + 5
      while not safety.offroad() and time.monotonic() < deadline:
        stop.wait(0.1)
      configure(args.action, safety)
      print("Astra link configuration saved. Restart of the optional daemon is not performed.")
    finally:
      stop.set()
    return 0
  except Exception:
    print("Astra link configuration failed; verify parked state and private configuration locally.", file=sys.stderr)
    return 1


if __name__ == "__main__":
  sys.exit(main())
