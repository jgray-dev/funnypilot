"""FunnyPilot v3.7.5 — carState mirror over /dev/shm for the feedback recorder.

WHY THIS EXISTS. msgq gives every service NUM_READERS = 15 subscriber slots
(msgq_repo/msgq/msgq.h). A 16th subscriber is not refused: msgq_init_subscriber()
zeroes the reader table and evicts all fifteen (msgq_repo/msgq/msgq.cc). Every
evicted reader re-registers on its next receive with its read pointer reset to
the write pointer, and as soon as all sixteen are back the next registration
evicts everyone again. A 20 Hz reader such as calibrationd or locationd is then
evicted between almost every two receives and never sees a carState message at
all, so liveCalibration publishes valid=false (calibrationd takes validity from
sm.all_checks()), livePose publishes inputsOK=false, and liveParameters,
liveTorqueParameters and liveDelay follow. selfdrived then refuses to engage.

Normal C3X driving already had fifteen carState readers when 3.7.1a's feedback
recorder (sunnypilot/feedback/feedbackd.py) subscribed as the sixteenth. The
recorder needs carState — driver torque, pedals, speed, steering angle exist in
no other service — but it must not hold a reader slot for it. So card publishes
the scalars here, right after it sends the same CarState over msgq, at the same
100 Hz. This is the fork's established pattern for telemetry that may touch
neither the capnp schema (a rebuild the prebuilt fork cannot ship) nor IPC
capacity: see long_v2/scc_shm.py and car/brake_light_shm.py.

Format: one line, positional, comma-separated:
  fpcs1,<writer time.monotonic()>,<canValid>,<vEgo>,<vEgoRaw>,<aEgo>,
  <steeringAngleDeg>,<steeringTorque>,<steeringPressed>,<brakePressed>,
  <gasPressed>,<standstill>
Booleans are 0/1. The tag pins the layout: a reader that sees any other tag
reads nothing rather than a different quantity. Grow it only by appending
fields under a new tag.

STALENESS IS LOAD-BEARING, as in the other channels: a dead card must read as
"no carState", never as a stuck one. SubMaster declares a 100 Hz service dead
after 10 periods, and the reader keeps that bound so the recorder's
`valid.carState` means what it meant when it came from a subscription.

DIAGNOSTIC ONLY. Nothing that reads this can affect control, the writer never
raises into card, and nothing touches the filesystem at import.
"""
import math
import os
import time

SHM_PATH = '/dev/shm/fp_carstate'
TAG = 'fpcs1'
FIELDS = 12
STALE_S = 0.1  # 10 periods of the 100 Hz writer, SubMaster's own alive bound


def format_line(mono, can_valid, v_ego, v_ego_raw, a_ego, angle_deg, torque,
                steering_pressed, brake_pressed, gas_pressed, standstill):
  return (f"{TAG},{mono:.4f},{int(bool(can_valid))},{v_ego:.4f},{v_ego_raw:.4f},{a_ego:.4f}," +
          f"{angle_deg:.3f},{torque:.2f},{int(bool(steering_pressed))},{int(bool(brake_pressed))}," +
          f"{int(bool(gas_pressed))},{int(bool(standstill))}")


def parse_line(raw):
  """A record dict, or None for a wrong tag, a short or long line, or a non-finite number."""
  parts = raw.strip().split(',')
  if len(parts) != FIELDS or parts[0] != TAG:
    return None
  try:
    mono, v, vr, a, ang, tq = (float(parts[i]) for i in (1, 3, 4, 5, 6, 7))
    flags = [int(parts[i]) for i in (2, 8, 9, 10, 11)]
  except ValueError:
    return None
  if not all(math.isfinite(x) for x in (mono, v, vr, a, ang, tq)) or any(f not in (0, 1) for f in flags):
    return None
  return {'observed': mono, 'can_valid': bool(flags[0]), 'v_ego': v, 'v_ego_raw': vr, 'a_ego': a,
          'steering_angle_deg': ang, 'steering_torque': tq, 'steering_pressed': bool(flags[1]),
          'brake_pressed': bool(flags[2]), 'gas_pressed': bool(flags[3]), 'standstill': bool(flags[4])}


class CarStateTapPublisher:
  """card's side: call update(CS) once per published CarState. Best-effort; never raises.

  A fixed temp name instead of mkstemp: there is exactly one writer, and at
  100 Hz the entropy read and O_EXCL retry loop buy nothing. The rename is
  still atomic, so a reader never sees a torn line.
  """

  def __init__(self, path=None):
    self.path = SHM_PATH if path is None else path
    self.tmp = self.path + '.tmp'

  def update(self, CS, mono=None):
    try:
      line = format_line(time.monotonic() if mono is None else mono, CS.canValid,
                         float(CS.vEgo), float(CS.vEgoRaw), float(CS.aEgo),
                         float(CS.steeringAngleDeg), float(CS.steeringTorque),
                         CS.steeringPressed, CS.brakePressed, CS.gasPressed, CS.standstill)
      with open(self.tmp, 'w') as f:
        f.write(line)
      os.replace(self.tmp, self.path)
    except Exception:
      try:
        os.unlink(self.tmp)
      except Exception:
        pass


class CarStateTapReader:
  """The recorder's side. poll(now) returns the current record only while it is fresh.

  `latest` keeps the last well-formed record at any age for callers that apply
  their own window (protocol.publish_motion); a stale record is never handed
  out as a sample.
  """

  def __init__(self, path=None, stale_s=STALE_S):
    self.path = SHM_PATH if path is None else path
    self.stale_s = stale_s
    self.latest = None

  def poll(self, now):
    try:
      with open(self.path) as f:
        record = parse_line(f.read(512))
    except (OSError, ValueError):
      return None
    if record is None:
      return None
    self.latest = record
    age = now - record['observed']
    if not -1.0 < age <= self.stale_s:  # a stamp from the future lands here too
      return None
    return record
