#!/usr/bin/env python3

import os
import time
from datetime import datetime
from pathlib import Path

import cereal.messaging as messaging

from openpilot.common.realtime import config_realtime_process, Priority
from openpilot.common.swaglog import cloudlog

LOG_ROOT = Path("/data/openpilot/can_sniffer")
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB cap per session
ACCEL_THRESHOLD = 0.3  # m/s^2
VEGO_THRESHOLD = 1.0   # m/s (~2 mph)


def should_capture(car_state) -> bool:
  if car_state is None:
    return False
  if car_state.aEgo > ACCEL_THRESHOLD:
    return True
  if car_state.gasPressed and car_state.vEgo > VEGO_THRESHOLD:
    return True
  return False


def ensure_log_dir() -> Path:
  LOG_ROOT.mkdir(parents=True, exist_ok=True)
  return LOG_ROOT


def new_log_file() -> Path:
  base = ensure_log_dir()
  ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
  return base / f"can_sniff_{ts}.log"


def write_header(handle) -> None:
  handle.write("# monotime_ns,bus,address_hex,data_hex,vEgo_mps,aEgo_mps2\n")
  handle.flush()


def main() -> None:
  config_realtime_process(5, Priority.BEST_EFFORT)
  sm = messaging.SubMaster(['can', 'carState'], ignore_avg_freqs={'can'})

  log_path = new_log_file()
  cloudlog.info(f"CAN sniffer logging to {log_path}")

  with open(log_path, 'w', buffering=1) as handle:
    write_header(handle)
    last_flush = time.monotonic()

    while True:
      sm.update(100)

      car_state = sm['carState'] if sm.alive['carState'] else None
      capturing = should_capture(car_state)

      if capturing and sm.updated['can']:
        log_ts = sm.logMonoTime['can']
        v_ego = car_state.vEgo if car_state is not None else 0.0
        a_ego = car_state.aEgo if car_state is not None else 0.0

        for msg in sm['can'].can:
          if msg.src not in (0, 1):
            continue
          handle.write(f"{log_ts},{msg.src},{msg.address:#x},{msg.dat.hex()},{v_ego:.3f},{a_ego:.3f}\n")

      now = time.monotonic()
      if now - last_flush > 1.0:
        handle.flush()
        last_flush = now

      if handle.tell() >= MAX_FILE_BYTES:
        cloudlog.info("CAN sniffer reached file cap; stopping capture for this session.")
        break


if __name__ == "__main__":
  try:
    main()
  except Exception:
    cloudlog.exception("CAN sniffer crashed")
    raise
