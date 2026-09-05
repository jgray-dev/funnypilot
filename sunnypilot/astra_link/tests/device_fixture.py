"""No-native fixture: production device/operations against a loopback Worker only."""

import json
import os
import re
import signal
import threading
import urllib.request
from pathlib import Path

from sunnypilot.astra_link.core import Journal, canonical
from sunnypilot.astra_link.daemon import Device
from sunnypilot.astra_link.execution import probe_identity
from sunnypilot.astra_link.files import Files


def main():
  origin = os.environ["TEST_ORIGIN"]
  if not re.fullmatch(r"http://127\.0\.0\.1:\d+", origin):
    raise ValueError("loopback fixture required")
  root = Path(os.environ["ASTRA_FIXTURE_ROOT"])
  token = os.environ["ASTRA_FIXTURE_TOKEN"]
  stop = threading.Event()
  signal.signal(signal.SIGTERM, lambda *_: stop.set())

  class FixtureTransport:
    def post(self, route, value):
      request = urllib.request.Request(origin + "/device/" + route, data=canonical(value),
                                       headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
      with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read(65537))

  class FixtureSafety:
    def snapshot(self):
      mode = (root / "mode").read_text().strip()
      return {"mode": mode, "reason": "synthetic fixture, not vehicle evidence"}

    def offroad(self):
      return not stop.is_set() and self.snapshot()["mode"] == "offroad"

  device = Device(FixtureTransport(), FixtureSafety(), journal=Journal(str(root / "private" / "receipts.json")),
                  files=Files(roots=(str(root / "device"), str(root / "logs")), recordings=()),
                  identity=lambda: probe_identity(str(root / "device")))
  while not stop.is_set():
    try:
      device.step()
    except Exception:
      device.uncertain()
    stop.wait(0.15)
  # Finish an acknowledged fixture result when shutdown follows a completed command.
  if device.pending is not None:
    try:
      device.transport.post("result", device.pending)
    except Exception:
      pass


if __name__ == "__main__":
  main()
