"""FunnyPilot v3.2.7 — triage flight recorder.

Persistent, size-capped JSONL logging built to root-cause the recurring
"smoothing feels turned off after the car sits parked" report. The 3.2.5st
updater-revert guards addressed one vector; the symptom returned, so 3.2.7
records enough evidence to discriminate between the remaining hypotheses:

  A. CODE SWAP  — what is on disk changed while parked / at boot
                  (boot + pulse records from nav_webserver: branch, commit,
                  dirty flag, file hashes, updater state, every 10 min).
  B. RUNTIME DEGRADATION — the code is fine but lat_interp loses sub-frame
                  headroom (health < ~4) or falls into its fallbacks
                  (1 Hz onroad records from controlsd).
  C. TUNING DRIFT — live learners (torque params, angle offset, lateral
                  delay) moved while parked, changing the feel with
                  identical code (context block every 10 s in the 1 Hz log).

Plus user-aligned ground truth: the web UI "Mark issue" button appends a
timestamped marker so the subjective moment can be lined up with the data.

Design constraints: never throw into the control loop (all IO wrapped),
never grow unbounded (size-based rotation, .1 backup), import-light
(stdlib only) so it is testable without the openpilot environment.
"""
import json
import os
import time

TRIAGE_DIR = "/data/funnypilot_triage"
DEFAULT_MAX_BYTES = 4 * 1024 * 1024  # per file; one .1 backup is kept


class TriageRecorder:
  """Append-only JSONL writer with size-based rotation. All IO is best-effort."""

  def __init__(self, name: str, directory: str = TRIAGE_DIR, max_bytes: int = DEFAULT_MAX_BYTES):
    self.path = os.path.join(directory, f"{name}.jsonl")
    self.directory = directory
    self.max_bytes = max_bytes
    self._size = None  # lazily discovered

  def _rotate_if_needed(self) -> None:
    if self._size is None:
      try:
        self._size = os.path.getsize(self.path)
      except OSError:
        self._size = 0
    if self._size >= self.max_bytes:
      try:
        os.replace(self.path, self.path + ".1")
      except OSError:
        pass
      self._size = 0

  def write(self, record: dict) -> bool:
    try:
      os.makedirs(self.directory, exist_ok=True)
      self._rotate_if_needed()
      record.setdefault("t", round(time.time(), 2))  # noqa: TID251 (wall clock is correct for log records)
      line = json.dumps(record, separators=(",", ":")) + "\n"
      with open(self.path, "a") as f:
        f.write(line)
      self._size = (self._size or 0) + len(line)
      return True
    except Exception:
      return False


class LatInterpMonitor:
  """Aggregates per-frame controls samples into 1 Hz JSONL records.

  Fields per record:
    n        frames aggregated (~100 when healthy)
    la/lo    fraction of frames with lat/long active
    hmin/havg  min/mean lat_interp health_frames (5 = healthy, <4 = degrading)
    v        mean v_ego
    lc       any lane-change frames (SETTLE forced linear)
    clim     frames where curvature was clipped by ISO limits
    at/ac    last long plan aTarget / commanded accel (smoothing sanity)
    ctx      every CONTEXT_EVERY records: live tuning snapshot (hypothesis C)

  Lateral-oscillation evidence (the "bite then loosen" report):
    sp       fraction of frames with CS.steeringPressed
    spe      steeringPressed RISING EDGES this second — a driver-override
             limit cycle shows up directly as spe >= 2 while hands are off
    ovr      MINIMUM driver-override torque scale this second (1.0 = no
             softening; 0.6 = fully softened — a 40% torque cut)
    sat      fraction of frames with the lat controller output saturated
    slb      fraction of frames with steer_limited_by_safety
    tqx      max |commanded steer torque| this second (normalized 0..1)
  """

  PERIOD = 1.0
  CONTEXT_EVERY = 10

  def __init__(self, recorder: TriageRecorder):
    self.recorder = recorder
    self._emits = 0
    self._t0 = None
    self._sp_prev = False  # persists across records so edges spanning seconds count once
    self._reset_acc()

  def _reset_acc(self):
    self._n = 0
    self._lat_active = 0
    self._long_active = 0
    self._health_min = None
    self._health_sum = 0.0
    self._v_sum = 0.0
    self._lane_change = False
    self._curv_limited = 0
    self._a_target = 0.0
    self._accel = 0.0
    self._sp = 0
    self._sp_edges = 0
    self._ovr_min = 1.0
    self._sat = 0
    self._slb = 0
    self._tq_max = 0.0

  def sample(self, mono_t: float, lat_active: bool, long_active: bool, v_ego: float, health_frames: float,
             lane_change: bool, curvature_limited: bool, a_target: float, accel: float,
             steering_pressed: bool = False, override_scale: float = 1.0, saturated: bool = False,
             steer_limited: bool = False, torque: float = 0.0, context_fn=None) -> None:
    try:
      if self._t0 is None:
        self._t0 = mono_t
      self._n += 1
      self._lat_active += int(lat_active)
      self._long_active += int(long_active)
      h = float(health_frames)
      self._health_min = h if self._health_min is None else min(self._health_min, h)
      self._health_sum += h
      self._v_sum += float(v_ego)
      self._lane_change = self._lane_change or bool(lane_change)
      self._curv_limited += int(curvature_limited)
      self._a_target = float(a_target)
      self._accel = float(accel)
      self._sp += int(steering_pressed)
      if steering_pressed and not self._sp_prev:
        self._sp_edges += 1
      self._sp_prev = bool(steering_pressed)
      self._ovr_min = min(self._ovr_min, float(override_scale))
      self._sat += int(saturated)
      self._slb += int(steer_limited)
      self._tq_max = max(self._tq_max, abs(float(torque)))

      if mono_t - self._t0 < self.PERIOD:
        return

      rec = {
        "n": self._n,
        "la": round(self._lat_active / self._n, 2),
        "lo": round(self._long_active / self._n, 2),
        "hmin": round(self._health_min, 2),
        "havg": round(self._health_sum / self._n, 2),
        "v": round(self._v_sum / self._n, 1),
        "lc": int(self._lane_change),
        "clim": self._curv_limited,
        "at": round(self._a_target, 3),
        "ac": round(self._accel, 3),
        "sp": round(self._sp / self._n, 2),
        "spe": self._sp_edges,
        "ovr": round(self._ovr_min, 2),
        "sat": round(self._sat / self._n, 2),
        "slb": round(self._slb / self._n, 2),
        "tqx": round(self._tq_max, 3),
      }
      if context_fn is not None and self._emits % self.CONTEXT_EVERY == 0:
        try:
          rec["ctx"] = context_fn()
        except Exception:
          rec["ctx"] = None
      self.recorder.write(rec)
      self._emits += 1
      self._t0 = mono_t
      self._reset_acc()
    except Exception:
      # never let telemetry break controls
      self._t0 = mono_t if self._t0 is None else self._t0
