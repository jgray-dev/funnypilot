"""Explain missing driving readiness even when no engage request was recognized.

Display-only: consumes existing messages, never changes engagement or calibration.
"""
import math
import re


def fresh(sm, service, started_frame):
  return (service in sm.valid and sm.valid[service] and sm.alive[service] and
          sm.recv_frame[service] >= started_frame)


def availability_message(sm, started_frame, elapsed):
  if elapsed < 5:
    return None
  if not fresh(sm, 'selfdriveState', started_frame):
    return ('Driving status unavailable', 'Waiting for valid selfdrived data')
  if not fresh(sm, 'liveCalibration', started_frame):
    return ('Calibration unavailable', 'Waiting for valid calibration data')
  calib = sm['liveCalibration']
  status = str(calib.calStatus)
  if status in ('uncalibrated', 'recalibrating'):
    progress = min(100, max(0, int(calib.calPerc)))
    return ('Calibration in progress' if status == 'uncalibrated' else 'Recalibrating', f'{progress}% complete. Engagement blocked')
  if status != 'calibrated' or len(calib.rpyCalib) != 3 or not all(math.isfinite(v) for v in calib.rpyCalib):
    return ('Calibration invalid', 'Check mounting and calibration when parked')

  # Permanent events reach the UI even if an engage-button edge never reaches
  # the state machine. Use both stock and MADS lists; don't infer from severity.
  blockers = []
  for service in ('onroadEvents', 'onroadEventsSP'):
    if fresh(sm, service, started_frame):
      events = sm[service] if service == 'onroadEvents' else sm[service].events
      for event in events:
        if event.noEntry or event.softDisable or event.immediateDisable:
          name = str(event.name)
          if name not in blockers:
            blockers.append(name)
  if blockers and not sm['selfdriveState'].enabled:
    if 'processNotRunning' in blockers and fresh(sm, 'managerState', started_frame):
      stopped = [p.name for p in sm['managerState'].processes if p.shouldBeRunning and not p.running]
      if stopped:
        return ('Process not running', ', '.join(stopped[:3]))
    # Keep the actual event name recognizable for diagnosis, without requiring
    # the UI to import the controller's heavyweight event callbacks.
    reason = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', blockers[0]).capitalize()
    extra = f' (+{len(blockers)-1} more)' if len(blockers) > 1 else ''
    return ('Engagement blocked', reason + extra)
  for service in ('modelV2', 'controlsState', 'longitudinalPlan'):
    if not fresh(sm, service, started_frame):
      return ('Driving data unavailable', f'Waiting for valid {service}')
  if not sm['selfdriveState'].enabled and not sm['selfdriveState'].engageable:
    return ('Engagement blocked', 'Waiting for controller refusal details')
  return None
