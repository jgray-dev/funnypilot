"""Readiness for projecting model geometry onto the camera image."""
import math


def model_overlay_ready(sm, started_frame):
  for service in ('liveCalibration', 'modelV2'):
    if not sm.valid[service] or not sm.alive[service] or sm.recv_frame[service] < started_frame:
      return False
  calibration = sm['liveCalibration']
  return (str(calibration.calStatus) == 'calibrated' and len(calibration.rpyCalib) == 3 and
          all(math.isfinite(angle) for angle in calibration.rpyCalib))
