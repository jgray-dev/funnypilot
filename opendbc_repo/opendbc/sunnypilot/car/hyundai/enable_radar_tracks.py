"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

FunnyPilot v3.3.1: the enable handshake is now EVIDENCED and VERIFIED.
Upstream logged only to carlog (invisible on-device), fetched the write
response with timeout=0 without checking it, and never read the config
back — so "successfully enabled" could be reported when the radar had
actually NACKed the write. Now every step (session control, radar identity
DIDs, current config, write ack, post-write read-back) is appended as JSONL
to /data/funnypilot_triage/radar_enable.jsonl (visible in the web UI Logs),
and success is claimed ONLY when the read-back shows the tracks bit set.
"""
import json
import os
import time

from opendbc.car import uds
from opendbc.car.carlog import carlog
from opendbc.car.isotp_parallel_query import IsoTpParallelQuery

DEVELOPER_DIAGNOSTIC = 0x07
CUSTOM_DIAGNOSTIC_REQUEST = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL, DEVELOPER_DIAGNOSTIC])
CUSTOM_DIAGNOSTIC_RESPONSE = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL + 0x40, DEVELOPER_DIAGNOSTIC])

READ_DATA_REQUEST = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER])
READ_DATA_RESPONSE = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40])

WRITE_DATA_REQUEST = bytes([uds.SERVICE_TYPE.WRITE_DATA_BY_IDENTIFIER])
WRITE_DATA_RESPONSE = bytes([uds.SERVICE_TYPE.WRITE_DATA_BY_IDENTIFIER + 0x40])

CONFIG_DATA_ID = bytes([0x01, 0x42])
DEFAULT_CONFIG = bytes([0x00, 0x00, 0x00, 0x01, 0x00, 0x00])
TRACKS_ENABLED_CONFIG = bytes([0x00, 0x00, 0x00, 0x01, 0x00, 0x01])
TRACKS_ENABLED_CONFIG_BYTES = b"\x00\x00\x01\x00\x01"

# radar identity DIDs, read once per enable run — the "device fingerprint"
# needed to match a DL3 radar against known-good configs if the standard
# payload is rejected (all best-effort; absent answers are logged as null)
IDENT_DIDS = {
  "app_sw": bytes([0xf1, 0x81]),    # application software identification
  "part_no": bytes([0xf1, 0x87]),   # spare part number
  "hkg_ver": bytes([0xf1, 0x00]),   # HKG version blob (same DID fw fingerprinting uses)
}

TRIAGE_DIR = "/data/funnypilot_triage"
LOG_NAME = "radar_enable.jsonl"
LOG_MAX_BYTES = 512 * 1024


def _fp_log(record: dict) -> None:
  """FunnyPilot: best-effort JSONL evidence; must never raise."""
  try:
    os.makedirs(TRIAGE_DIR, exist_ok=True)
    path = os.path.join(TRIAGE_DIR, LOG_NAME)
    if os.path.exists(path) and os.path.getsize(path) >= LOG_MAX_BYTES:
      os.replace(path, path + ".1")
    record.setdefault("t", round(time.time(), 2))
    with open(path, "a") as f:
      f.write(json.dumps(record, separators=(",", ":")) + "\n")
  except Exception:
    pass


def _read_did(logcan, sendcan, bus: int, addr: int, did: bytes, timeout: float) -> str | None:
  """Read one data identifier; returns payload hex or None."""
  try:
    query = IsoTpParallelQuery(sendcan, logcan, bus, [addr], [READ_DATA_REQUEST + did], [READ_DATA_RESPONSE])
    for _, data in query.get_data(timeout).items():
      return data[3:].hex()
  except Exception:
    pass
  return None


def enable_radar_tracks(logcan, sendcan, bus=0, addr=0x7d0, timeout=0.1, retry=2):
  carlog.error("radar_tracks: enabling ...")

  for i in range(retry):
    step: dict = {"kind": "enable_attempt", "try": i + 1, "bus": bus, "addr": addr}
    try:
      query = IsoTpParallelQuery(sendcan, logcan, bus, [addr], [CUSTOM_DIAGNOSTIC_REQUEST], [CUSTOM_DIAGNOSTIC_RESPONSE])
      session_resps = query.get_data(timeout)
      step["session"] = len(session_resps) > 0
      if not session_resps:
        # radar didn't answer the developer-session request at all
        _fp_log(step)
        carlog.error(f"radar_tracks retry ({i + 1}) ...")
        continue

      # radar identity — the device fingerprint (first attempt only)
      if i == 0:
        step["ident"] = {name: _read_did(logcan, sendcan, bus, addr, did, timeout) for name, did in IDENT_DIDS.items()}

      # current config
      request = READ_DATA_REQUEST + CONFIG_DATA_ID
      query = IsoTpParallelQuery(sendcan, logcan, bus, [addr], [request], [READ_DATA_RESPONSE])
      current_config = None
      for _, data in query.get_data(timeout).items():
        current_config = data[3:]
      step["config"] = current_config.hex() if current_config is not None else None
      carlog.error(f"radar_tracks: current config: {step['config']}")

      already = current_config is not None and current_config in (TRACKS_ENABLED_CONFIG, TRACKS_ENABLED_CONFIG_BYTES)
      if already:
        step["action"] = "already_enabled"
        step["enabled"] = True
        _fp_log(step)
        carlog.error("radar_tracks: already enabled, skipping ...")
        return True

      # write the tracks-enabled config and CHECK the ack (upstream used
      # timeout=0 and ignored the response entirely)
      step["action"] = "write"
      request = WRITE_DATA_REQUEST + CONFIG_DATA_ID + TRACKS_ENABLED_CONFIG
      query = IsoTpParallelQuery(sendcan, logcan, bus, [addr], [request], [WRITE_DATA_RESPONSE])
      step["write_ack"] = len(query.get_data(timeout)) > 0

      # verify: read the config back — success is what the radar SAYS, not hope
      verify = None
      query = IsoTpParallelQuery(sendcan, logcan, bus, [addr], [READ_DATA_REQUEST + CONFIG_DATA_ID], [READ_DATA_RESPONSE])
      for _, data in query.get_data(timeout).items():
        verify = data[3:]
      step["verify"] = verify.hex() if verify is not None else None

      enabled = verify is not None and (verify in (TRACKS_ENABLED_CONFIG, TRACKS_ENABLED_CONFIG_BYTES) or verify.endswith(b"\x01"))
      step["enabled"] = bool(enabled)
      _fp_log(step)

      if enabled:
        carlog.error("radar_tracks: successfully enabled (verified)")
        return True
      carlog.error("radar_tracks: write did not take (see radar_enable.jsonl)")

    except Exception as e:
      step["exc"] = repr(e)[:200]
      _fp_log(step)
      carlog.exception(f"radar_tracks exception: {e}")

    carlog.error(f"radar_tracks retry ({i + 1}) ...")

  _fp_log({"kind": "enable_result", "enabled": False})
  carlog.error("radar_tracks: failed")
  return False


if __name__ == "__main__":
  import cereal.messaging as messaging
  sendcan = messaging.pub_sock('sendcan')
  logcan = messaging.sub_sock('can')
  time.sleep(1)

  enabled = enable_radar_tracks(logcan, sendcan, bus=0, addr=0x7d0, timeout=0.1)
  print(f"enabled: {enabled}")
