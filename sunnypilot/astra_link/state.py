"""Independent native-state monitor; no native imports until explicitly started."""

import math
import threading
import time

from sunnypilot.feedback.protocol import MOTION, read_json


class Safety:
  def __init__(self, clock=time.monotonic):
    self.clock = clock
    self.lock = threading.Lock()
    self.mode = "unknown"
    self.reason = "native state unavailable"
    self.observed = float("-inf")
    self.valid_until = float("-inf")
    self.stationary = None
    self.engaged = None
    self.motion_until = float("-inf")
    self.engagement_until = float("-inf")

  def update(self, sm, started=False):
    # `started` is manager's IsOnroad param, the same transition deviceState.started
    # reports. It is read from Params rather than a deviceState subscription:
    # deviceState is at msgq's 15-reader limit with sunnylink registered, and a
    # 16th reader evicts every other subscriber (see feedback/carstate_shm.py).
    # A stale or missing param can only ever add "onroad", never remove it.
    now = self.clock()
    mode, reason = "unknown", "native state stale or invalid"
    services = ("pandaStates",)
    if all(sm.seen[s] and sm.valid[s] and 0 <= now - sm.recv_time[s] <= 2 for s in services):
      pandas = sm["pandaStates"]
      if started or any(p.ignitionLine or p.ignitionCan for p in pandas):
        mode, reason = "onroad", "started or ignition on"
      elif pandas and all(str(p.pandaType) != "unknown" for p in pandas):
        mode, reason = "offroad", "fresh known pandas with ignition off"
    stationary, engaged = None, None
    motion_until = engagement_until = float("-inf")
    # feedbackd already consumes carState; subscribing here would exceed msgq's
    # reader budget. /dev/shm clears on reboot; age only the original monotonic
    # receive time, so a stalled publisher cannot keep stationary permission alive.
    motion = read_json(MOTION, limit=512)
    if isinstance(motion, dict):
      observed = motion.get("observed")
      if type(observed) in (int, float) and math.isfinite(observed) and 0 <= now - observed <= 2:
        if type(motion.get("stationary")) is bool:
          stationary = motion["stationary"]
          motion_until = observed + 2
    engagement_services = ("selfdriveState", "selfdriveStateSP", "carControl")
    if all(sm.seen[s] and sm.valid[s] and 0 <= now - sm.recv_time[s] <= 2 for s in engagement_services):
      driving, mads, control = sm["selfdriveState"], sm["selfdriveStateSP"].mads, sm["carControl"]
      engaged = bool(driving.enabled or driving.active or mads.enabled or mads.active or
                     control.enabled or control.latActive or control.longActive)
      engagement_until = min(sm.recv_time[s] for s in engagement_services) + 2
    with self.lock:
      self.mode, self.reason, self.observed = mode, reason, now
      self.valid_until = min(sm.recv_time[s] for s in services) + 2
      self.stationary, self.engaged = stationary, engaged
      self.motion_until, self.engagement_until = motion_until, engagement_until

  def snapshot(self):
    with self.lock:
      now = self.clock()
      if not 0 <= now - self.observed <= 2:
        return {"mode": "unknown", "reason": "native monitor stale", "stationary": None, "engaged": None}
      return {"mode": self.mode if now <= self.valid_until else "unknown",
              "reason": self.reason if now <= self.valid_until else "native mode stale",
              "stationary": self.stationary if now <= self.motion_until else None,
              "engaged": self.engaged if now <= self.engagement_until else None}

  def offroad(self):
    return self.snapshot()["mode"] == "offroad"

  def can_modify(self):
    state = self.snapshot()
    return state["stationary"] is True and state["engaged"] is False

  def monitor(self, stop):
    try:
      import cereal.messaging as messaging
      from openpilot.common.params import Params
      params = Params()
      sm = messaging.SubMaster(["pandaStates", "selfdriveState", "selfdriveStateSP", "carControl"])
      while not stop.is_set():
        sm.update(100)
        self.update(sm, bool(params.get_bool("IsOnroad")))
    except Exception:
      # Failure becomes unknown immediately, not a cached parked permission.
      with self.lock:
        self.mode, self.reason = "unknown", "native monitor failed"
        self.stationary = self.engaged = None

  def start(self, stop):
    thread = threading.Thread(target=self.monitor, args=(stop,), daemon=True, name="astra-native-state")
    thread.start()
    return thread
