"""Independent native-state monitor; no native imports until explicitly started."""

import threading
import time


class Safety:
  def __init__(self, clock=time.monotonic):
    self.clock = clock
    self.lock = threading.Lock()
    self.mode = "unknown"
    self.reason = "native state unavailable"
    self.observed = float("-inf")
    self.valid_until = float("-inf")

  def update(self, sm):
    now = self.clock()
    mode, reason = "unknown", "native state stale or invalid"
    services = ("deviceState", "pandaStates")
    if all(sm.seen[s] and sm.valid[s] and 0 <= now - sm.recv_time[s] <= 2 for s in services):
      pandas = sm["pandaStates"]
      if sm["deviceState"].started or any(p.ignitionLine or p.ignitionCan for p in pandas):
        mode, reason = "onroad", "started or ignition on"
      elif pandas and all(str(p.pandaType) != "unknown" for p in pandas):
        mode, reason = "offroad", "fresh known pandas with ignition off"
    with self.lock:
      self.mode, self.reason, self.observed = mode, reason, now
      self.valid_until = min(sm.recv_time[s] for s in services) + 2

  def snapshot(self):
    with self.lock:
      if not 0 <= self.clock() - self.observed <= 2 or self.clock() > self.valid_until:
        return {"mode": "unknown", "reason": "native monitor stale"}
      return {"mode": self.mode, "reason": self.reason}

  def offroad(self):
    return self.snapshot()["mode"] == "offroad"

  def monitor(self, stop):
    try:
      import cereal.messaging as messaging
      sm = messaging.SubMaster(["deviceState", "pandaStates"])
      while not stop.is_set():
        sm.update(100)
        self.update(sm)
    except Exception:
      # Failure becomes unknown immediately, not a cached parked permission.
      with self.lock:
        self.mode, self.reason = "unknown", "native monitor failed"

  def start(self, stop):
    thread = threading.Thread(target=self.monitor, args=(stop,), daemon=True, name="astra-native-state")
    thread.start()
    return thread
