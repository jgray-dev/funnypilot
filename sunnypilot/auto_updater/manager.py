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


# v3.6.1: an OSM refresh DELETES the local database before re-downloading it,
# so it is a whole-database replacement and must be rare. Seven days keeps the
# data current without ever leaving the car mapless for the sake of it.
OSM_MIN_REFRESH_S = 7 * 24 * 3600


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
    """FunnyPilot v3.6.1 — RATE-LIMITED, AND THAT IS A BUG FIX, NOT A TUNING.

    THE DEFECT: this set `OsmDbUpdatesCheck` on the plain 15-minute cycle, and
    the first thing `mapd_manager.update_osm_db()` does with that flag is

        cleanup_old_osm_data(get_files_for_cleanup())

    which DELETES `{mapd_root}/db` and `{mapd_root}/v*` -- the entire offline
    OSM database -- before queueing the replacement download. So every 15
    minutes parked on wifi the device threw its map data away and started
    fetching several gigabytes again. Anything that interrupted that (going
    onroad, the wifi dropping, a slow download) left NO MAP DATA AT ALL: mapd
    cannot match a route, MapTargetVelocities is empty, the minimap is blank
    and SCC-M has nothing to act on. Reported as exactly that.

    A refresh is a WHOLE-DATABASE REPLACEMENT, so it must be rare. mapd already
    stamps `OsmDownloadedDate` on every request; that is the right clock to
    gate on, and it is WALL CLOCK (`datetime.now().timestamp()`), so it must be
    compared against `time.time()` and not against monotonic (v3.4.5 rule).
    """
    try:
      if not self.params.get_bool("OsmLocal"):
        return  # no region configured, nothing to refresh

      if self.params.get_bool("OsmDbUpdatesCheck"):
        return  # a refresh is already pending; re-arming it re-deletes the db

      age = self._map_data_age_s()
      if age < OSM_MIN_REFRESH_S:
        cloudlog.debug(f"auto_updater: map data is {age / 3600.0:.1f} h old, not refreshing")
        return

      cloudlog.warning(f"auto_updater: OSM data {age / 86400.0:.1f} days old, refreshing (DELETES local db)")
      self.params.put_bool("OsmDbUpdatesCheck", True)
    except Exception:
      cloudlog.exception("auto_updater: failed to refresh map data")

  def _map_data_age_s(self) -> float:
    """Seconds since the last OSM download request. `inf` if never, which lets
    a device that has no map data yet fetch one immediately."""
    try:
      raw = self.params.get("OsmDownloadedDate")
      if not raw:
        return float('inf')
      # WALL CLOCK ON PURPOSE, and the repo-wide ban on time.time() is right
      # to make this argue for itself: `OsmDownloadedDate` is written by mapd
      # as `datetime.now().timestamp()`, so it lives in the wall-clock domain
      # and monotonic cannot be compared against it. This is the corollary the
      # v3.4.5 clock rule calls out -- recv_time/monotonic is the safe default
      # only when BOTH sides are monotonic.
      return max(0.0, time.time() - float(raw))  # noqa: TID251
    except Exception:
      return float('inf')

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
