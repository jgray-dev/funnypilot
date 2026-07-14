#!/usr/bin/env python3
"""
FunnyPilot v3.3.3st: hands-free background refresh.

Offroad-gated by process_config.py (this process only runs while parked),
so all that's tracked here is WiFi continuity. Once WiFi has held for
OFFROAD_WIFI_MIN_S seconds, re-check the active driving model bundle and
the configured OSM map region for updates - the same actions the
Settings "CHECK"/"Database Update" buttons trigger manually - so both
stay current without the user remembering to open Settings. Re-arms every
OFFROAD_WIFI_MIN_S so a long parked/charging session keeps refreshing.
"""
import time

import cereal.messaging as messaging
from cereal import log
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.models.helpers import get_active_bundle

OFFROAD_WIFI_MIN_S = 15 * 60


class AutoUpdater:
  def __init__(self):
    self.params = Params()
    self.sm = messaging.SubMaster(['deviceState'])
    self.wifi_connected_since: float | None = None
    self.last_trigger_mono: float | None = None

  def _on_wifi(self) -> bool:
    self.sm.update(0)
    if not self.sm.valid['deviceState']:
      return False
    return self.sm['deviceState'].networkType == log.DeviceState.NetworkType.wifi

  def _refresh_active_model(self) -> None:
    try:
      active_bundle = get_active_bundle(self.params)
      if active_bundle is None:
        return
      if self.params.get("ModelManager_DownloadIndex") is not None:
        return  # a download is already in flight

      cloudlog.info(f"auto_updater: refreshing active model bundle {active_bundle.index}")
      self.params.put("ModelManager_DownloadIndex", active_bundle.index)
    except Exception:
      cloudlog.exception("auto_updater: failed to refresh active model")

  def _refresh_map_data(self) -> None:
    try:
      if not self.params.get_bool("OsmLocal"):
        return  # no region configured, nothing to refresh

      cloudlog.info("auto_updater: refreshing OSM map data")
      self.params.put_bool("OsmDbUpdatesCheck", True)
    except Exception:
      cloudlog.exception("auto_updater: failed to refresh map data")

  def _maybe_trigger(self) -> None:
    now = time.monotonic()

    if not self._on_wifi():
      self.wifi_connected_since = None
      return

    if self.wifi_connected_since is None:
      self.wifi_connected_since = now

    if (now - self.wifi_connected_since) < OFFROAD_WIFI_MIN_S:
      return

    if self.last_trigger_mono is not None and (now - self.last_trigger_mono) < OFFROAD_WIFI_MIN_S:
      return

    self.last_trigger_mono = now
    self._refresh_active_model()
    self._refresh_map_data()

  def run(self) -> None:
    rk = Ratekeeper(1, print_delay_threshold=None)
    while True:
      self._maybe_trigger()
      rk.keep_time()


def main():
  AutoUpdater().run()


if __name__ == "__main__":
  main()
