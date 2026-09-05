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

  v3.3.3 idle collapse: parked seconds (nothing active, v ~ 0) are not
  written per-second — they collapse into one {"idle": N} heartbeat per
  IDLE_HEARTBEAT_S, and the first driving record after an idle stretch
  carries "idl": N so no time is unaccounted for. A day parked is a
  handful of lines instead of tens of thousands.
  """

  PERIOD = 1.0
  CONTEXT_EVERY = 10
  IDLE_HEARTBEAT_S = 60.0
  IDLE_V_THRESHOLD = 0.5  # m/s

  def __init__(self, recorder: TriageRecorder):
    self.recorder = recorder
    self._emits = 0
    self._t0 = None
    self._sp_prev = False  # persists across records so edges spanning seconds count once
    self._idle_skipped = 0
    self._idle_last_write = None
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
    self._eps_min = 1.0
    self._dt_max = 0.0
    self._div_max = 0.0
    self._pitch_max = 0.0
    self._motion_scale_min = 1.0
    self._motion_credit_max = 0.0

  def sample(self, mono_t: float, lat_active: bool, long_active: bool, v_ego: float, health_frames: float,
             lane_change: bool, curvature_limited: bool, a_target: float, accel: float,
             steering_pressed: bool = False, override_scale: float = 1.0, saturated: bool = False,
             steer_limited: bool = False, torque: float = 0.0, eps_authority: float = 1.0,
             driver_torque: float = 0.0, torque_out: float | None = None,
             pitch_rate_deg: float = 0.0, context_fn=None,
             motion_scale: float = 1.0, motion_credit: float = 0.0) -> None:
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
      # v3.3.8: per-second MIN of the EPS governor authority bound (1.0 = the
      # driver-torque clamp never engaged; < 1.0 = the hardware limit was live)
      self._eps_min = min(self._eps_min, float(eps_authority))
      # v3.3.8 hypothesis discriminators (the 3.2.8-era "inertia trips the
      # sensor" story was NEVER verified on-road — these make it checkable):
      # dtx = per-second MAX |raw torsion-bar reading| (>50 = clamp band was
      # reachable, >150 = steeringPressed band); tqd = per-second MAX
      # |requested - applied| torque (carOutput lags one frame; sustained
      # large values = the carcontroller stripped our request).
      self._dt_max = max(self._dt_max, abs(float(driver_torque)))
      if torque_out is not None:
        self._div_max = max(self._div_max, abs(float(torque) - float(torque_out)))
      # v3.3.8: UNVERIFIED weight-transfer hypothesis (user's railroad-track
      # observation, NOT the driver-torque clamp — EPS authority stayed 100%
      # during those events). Car-frame Y-axis angular rate (approx. pitch
      # rate) peak/s, from livePose via controlsd's calibrated_pose — a bump
      # that unloads the front axle should show as a coherent spike here.
      # Correlate against "eps"/"dtx"/"sp" swings during the SAME second to
      # test whether it lines up with felt oscillation independent of torque.
      self._pitch_max = max(self._pitch_max, abs(float(pitch_rate_deg)))
      self._motion_scale_min = min(self._motion_scale_min, float(motion_scale))
      self._motion_credit_max = max(self._motion_credit_max, float(motion_credit))

      if mono_t - self._t0 < self.PERIOD:
        return

      # v3.3.3 idle collapse: parked + disengaged seconds are noise
      idle = (self._lat_active == 0 and self._long_active == 0 and
              self._sp == 0 and (self._v_sum / self._n) < self.IDLE_V_THRESHOLD)
      if idle:
        self._idle_skipped += 1
        if self._idle_last_write is None:
          self._idle_last_write = mono_t
        if mono_t - self._idle_last_write >= self.IDLE_HEARTBEAT_S:
          self.recorder.write({"idle": self._idle_skipped})
          self._idle_skipped = 0
          self._idle_last_write = mono_t
        self._t0 = mono_t
        self._reset_acc()
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
        "eps": round(self._eps_min, 2),
        "dtx": round(self._dt_max, 1),
        "tqd": round(self._div_max, 3),
        "pit": round(self._pitch_max, 1),
        "mcs": round(self._motion_scale_min, 3),  # minimum error-feedback scale
        "mcr": round(self._motion_credit_max, 4),  # maximum credit, m/s^2
      }
      if self._idle_skipped:
        rec["idl"] = self._idle_skipped  # idle seconds preceding this record
        self._idle_skipped = 0
      self._idle_last_write = None
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


class RadarTracksMonitor:
  """FunnyPilot v3.3.0e: 1 Hz JSONL evidence that the Mando radar-tracks
  enable worked, plus the data itself for longitudinal tuning.

  Fields per record:
    n / nmin / nmax  radar track points visible (this frame / min / max
                     over the second). n stuck at 0 while driving in
                     traffic => the 0x7D0 enable did not take.
    pts   up to 3 closest points as [dRel, yRel, vRel] (m, m, m/s)
    l1/l2 radarState leadOne/leadTwo as [dRel, vLead, aLeadK] or null
    cerr  frames with radar CAN errors this second

  Duck-typed access + full try/except: works with capnp readers and test
  stubs alike, and can never take radard down.

  v3.3.3 idle collapse: seconds with ZERO tracks all second collapse into
  one {"n":0,"idle":N,"cerr":total} heartbeat per IDLE_HEARTBEAT_S — the
  "n stuck at 0 while driving" evidence survives (N counts the seconds),
  without a parked car writing 86k identical lines a day.
  """

  PERIOD = 1.0
  MAX_PTS = 3
  IDLE_HEARTBEAT_S = 30.0

  def __init__(self, recorder: TriageRecorder):
    self.recorder = recorder
    self._t0 = None
    self._n_min = None
    self._n_max = 0
    self._cerr = 0
    self._idle_skipped = 0
    self._idle_cerr = 0
    self._idle_last_write = None

  def log_identity(self, cp) -> None:
    """One record at radard startup: the car + radar 'device fingerprint'.
    carFw comes from openpilot's ignition-time UDS firmware query, so the
    radar ECU's exact firmware version lands in the accessible log — what's
    needed to match this DL3 radar against known-good tracks configs. Plus
    radarUnavailable: False here means the 0x7D0 enable claimed success."""
    try:
      fw = []
      for f in getattr(cp, 'carFw', None) or []:
        try:
          fw.append({"ecu": str(f.ecu), "addr": int(f.address),
                     "fw": bytes(f.fwVersion).decode('utf-8', 'replace').replace('\x00', '').strip()})
        except Exception:
          continue
      self.recorder.write({
        "kind": "radar_identity",
        "car": str(getattr(cp, 'carFingerprint', '?')),
        "radarUnavailable": bool(getattr(cp, 'radarUnavailable', True)),
        "fw": fw,
      })
    except Exception:
      pass

  @staticmethod
  def _lead(lead) -> list | None:
    try:
      if lead is not None and bool(lead.status):
        return [round(float(lead.dRel), 1), round(float(lead.vLead), 1), round(float(lead.aLeadK), 2)]
    except Exception:
      pass
    return None

  def sample(self, mono_t: float, live_tracks, radar_state) -> None:
    try:
      if self._t0 is None:
        self._t0 = mono_t

      points = list(getattr(live_tracks, 'points', None) or [])
      n = len(points)
      self._n_min = n if self._n_min is None else min(self._n_min, n)
      self._n_max = max(self._n_max, n)
      try:
        self._cerr += int(bool(live_tracks.errors.canError))
      except Exception:
        pass

      if mono_t - self._t0 < self.PERIOD:
        return

      # v3.3.3 idle collapse: no tracks all second -> heartbeat, not a record
      if self._n_max == 0:
        self._idle_skipped += 1
        self._idle_cerr += self._cerr
        if self._idle_last_write is None:
          self._idle_last_write = mono_t
        if mono_t - self._idle_last_write >= self.IDLE_HEARTBEAT_S:
          self.recorder.write({"n": 0, "idle": self._idle_skipped, "cerr": self._idle_cerr})
          self._idle_skipped = 0
          self._idle_cerr = 0
          self._idle_last_write = mono_t
        self._t0 = mono_t
        self._n_min = None
        self._n_max = 0
        self._cerr = 0
        return

      pts = []
      try:
        for pt in sorted(points, key=lambda p: float(p.dRel))[:self.MAX_PTS]:
          pts.append([round(float(pt.dRel), 1), round(float(pt.yRel), 1), round(float(pt.vRel), 1)])
      except Exception:
        pts = []

      rec = {"n": n, "nmin": self._n_min, "nmax": self._n_max, "pts": pts, "cerr": self._cerr}
      if self._idle_skipped:
        rec["idl"] = self._idle_skipped  # zero-track seconds preceding this record
        self._idle_skipped = 0
        self._idle_cerr = 0
      self._idle_last_write = None
      try:
        rec["l1"] = self._lead(getattr(radar_state, 'leadOne', None))
        rec["l2"] = self._lead(getattr(radar_state, 'leadTwo', None))
      except Exception:
        rec["l1"] = rec["l2"] = None
      self.recorder.write(rec)

      self._t0 = mono_t
      self._n_min = None
      self._n_max = 0
      self._cerr = 0
    except Exception:
      # never let telemetry break radard
      self._t0 = mono_t if self._t0 is None else self._t0
