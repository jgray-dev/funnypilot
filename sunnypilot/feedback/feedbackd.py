#!/usr/bin/env python3
"""Always-run feedback capture; uploads are serialized and offroad/Wi-Fi only."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from openpilot.sunnypilot.feedback import protocol as P
from openpilot.sunnypilot.feedback.capture import Capture
from openpilot.sunnypilot.feedback.uploader import upload_event


def identity(repo):
  def git(*args):
    try:
      return subprocess.check_output(['git', '-c', f'safe.directory={repo}', '-C', str(repo), *args],
                                     timeout=3, stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.SubprocessError):
      return ''
  try:
    version = (repo / 'FUNNYPILOT_VERSION').read_text().strip()
  except OSError:
    version = ''
  return {'commit':git('rev-parse', 'HEAD'), 'branch':git('branch', '--show-current'), 'version':version,
          'dirty':bool(git('status', '--porcelain', '--untracked-files=no'))}


def snapshot(sm, now):
  cs, ctrl, lp = sm['carState'], sm['controlsState'], sm['longitudinalPlan']
  row = {'t':now, 'v':cs.vEgo, 'a':cs.aEgo, 'angle':cs.steeringAngleDeg,
         'driver_torque':cs.steeringTorque, 'steering_pressed':cs.steeringPressed,
         'brake_pressed':cs.brakePressed, 'gas_pressed':cs.gasPressed,
         'a_target':lp.aTarget, 'curvature':ctrl.curvature,
         'desired_curvature':ctrl.desiredCurvature,
         'long_source':str(lp.longitudinalPlanSource)}
  try:
    state = ctrl.lateralControlState
    row['lateral'] = getattr(state, state.which()).to_dict()
  except (AttributeError, RuntimeError):
    pass
  # Never copy the live estimator's potentially huge raw points cloud.
  torque = sm['liveTorqueParameters']
  row['torque_parameters'] = {k:getattr(torque, k) for k in
                              ('liveValid','latAccelFactorFiltered','latAccelOffsetFiltered','frictionCoefficientFiltered','version')}
  row['car_control'] = sm['carControl'].actuators.to_dict()
  row['lat_active'], row['long_active'] = sm['carControl'].latActive, sm['carControl'].longActive
  lead = sm['radarState'].leadOne
  row['lead'] = {k:getattr(lead, k) for k in ('status','dRel','vRel','vLeadK','aLeadK')}
  plan_sp = sm['longitudinalPlanSP']
  row['governor_source'] = str(plan_sp.longitudinalPlanSource)
  row['map_cap'] = plan_sp.smartCruiseControl.map.vTarget
  row['vision_cap'] = plan_sp.smartCruiseControl.vision.vTarget
  state, calib = sm['selfdriveState'], sm['liveCalibration']
  row['selfdrive'] = {'enabled':state.enabled, 'state':str(state.state), 'alert_type':state.alertType,
                      'alert_text1':state.alertText1, 'alert_text2':state.alertText2}
  row['calibration'] = {'status':str(calib.calStatus), 'percent':calib.calPerc, 'rpy':list(calib.rpyCalib),
                        'height':list(calib.height), 'wide_from_device':list(calib.wideFromDeviceEuler)}
  row['blocking_events'] = {}
  for service in ('onroadEvents', 'onroadEventsSP'):
    events = sm[service] if service == 'onroadEvents' else sm[service].events
    row['blocking_events'][service] = [str(e.name) for e in events if e.noEntry or e.softDisable or e.immediateDisable]
  # Include timestamps/validity so stale samples cannot masquerade as fresh data.
  row['valid'] = {s:bool(sm.valid[s] and sm.alive[s]) for s in
                  ('carState','controlsState','longitudinalPlan','selfdriveState','liveCalibration','onroadEvents','onroadEventsSP')}
  return row


RETRY_S = 5.0


def report_socket():
  sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
  try:
    try:
      os.unlink(P.SOCKET)
    except FileNotFoundError:
      pass
    sock.bind(P.SOCKET)
    os.chmod(P.SOCKET, 0o600)
    sock.setblocking(False)
    return sock
  except BaseException:
    sock.close()
    raise


def failure_status(cmd, now, message):
  # A full filesystem must not make the error-reporting path fail too.
  try:
    P.atomic_json(P.STATUS, {'id':cmd.get('id', '') if isinstance(cmd, dict) else '',
                             'saved':False, 'message':message, 't':now})
  except OSError:
    pass


class CaptureLoop:
  """Retry unavailable capture IO, not the control loop or the upload worker."""
  def __init__(self, log_root, code_identity, logger):
    self.log_root, self.code_identity, self.logger = log_root, code_identity, logger
    self.capture = self.sock = None
    self.pending_report = None
    self.retry_at = self.last_sample = 0.0

  def tick(self, now, sample, route):
    if now < self.retry_at:
      return
    cmd = None
    try:
      if self.capture is None:
        self.capture = Capture(log_root=self.log_root, identity=self.code_identity)
      if self.sock is None:
        self.sock = report_socket()
      if self.pending_report is not None:
        cmd, current_route, accepted_at = self.pending_report
        # Only a request that passed Capture's freshness check can get here.
        # Retrying its IO is not a newly arrived (now stale) user command.
        self.capture.report(cmd, current_route, accepted_at)
        self.pending_report = None
        cmd = None
      if sample is not None and now - self.last_sample >= .01:
        self.capture.sample(sample())
        self.last_sample = now
      # Bound work per tick even if a broken client floods the socket.
      for _ in range(8):
        try:
          raw = self.sock.recv(2048)
        except BlockingIOError:
          break
        except OSError:
          self.sock.close()
          self.sock = None
          raise
        cmd = {}
        try:
          cmd = json.loads(raw)
          current_route = route() or ''
          if isinstance(current_route, bytes):
            current_route = current_route.decode()
          try:
            self.capture.report(cmd, current_route, now)
          except OSError:
            self.pending_report = (cmd, current_route, now)
            raise
        except (ValueError, TypeError) as e:
          failure_status(cmd, now, str(e)[:120])
        cmd = None
      self.capture.finish(now)
    except (OSError, ValueError):
      # Keep the Capture object and durable queue. A retry must not recover our
      # own active reports as if the process had restarted, or discard labels.
      self.retry_at = now + RETRY_S
      if self.capture is not None:
        for event in self.capture.active.values():
          event['capture_interrupted'] = True
      if cmd is not None:
        failure_status(cmd, now, 'Feedback storage unavailable; save not confirmed. Retrying.')
      self.logger.exception('feedback capture unavailable; retrying')

  def close(self):
    if self.sock is not None:
      self.sock.close()


def main():
  import cereal.messaging as messaging
  from cereal import log
  from openpilot.common.params import Params
  from openpilot.system.hardware.hw import Paths
  from openpilot.common.swaglog import cloudlog

  try:
    os.nice(10)
  except OSError:
    cloudlog.exception('feedback scheduling priority unavailable')
  params = Params()
  capture_loop = CaptureLoop(Paths.log_root(), identity(Path(__file__).resolve().parents[2]), cloudlog)
  sm = messaging.SubMaster(['carState','controlsState','longitudinalPlan','deviceState','selfdriveState',
                           'liveTorqueParameters','carControl','radarState','longitudinalPlanSP','liveCalibration',
                           'onroadEvents','onroadEventsSP'], poll='carState')
  upload_allowed = False
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='feedback-upload')
  future = None
  next_upload = 0.0

  def upload_queued():
    capture = capture_loop.capture
    config = P.read_json(str(capture.root / 'cloud.json'), {})
    if not config.get('token'):
      return
    for path in sorted(capture.events.glob('*/event.json')):
      if not upload_allowed:
        break
      event = P.read_json(str(path), {})
      if event.get('state') != 'queued':
        continue
      try:
        upload_event(path.parent, config, Paths.log_root(), allowed=lambda: upload_allowed)
      except Exception as e:
        event['upload_error'] = type(e).__name__  # never persist credentials/response bodies
        P.atomic_json(str(path), event, durable=True)
        break

  try:
    while True:
      sm.update(50)
      now = time.monotonic()
      upload_allowed = (sm.valid['deviceState'] and sm.alive['deviceState'] and
                        not sm['deviceState'].started and
                        sm['deviceState'].networkType == log.DeviceState.NetworkType.wifi)
      capture_loop.tick(now, (lambda now=now: snapshot(sm, now)) if sm.updated['carState'] else None,
                        lambda: params.get('CurrentRoute'))
      if future is not None and future.done():
        try:
          future.result()
        except Exception:
          cloudlog.exception('feedback upload worker failed')
        future = None
      if upload_allowed and capture_loop.capture is not None and future is None and now >= next_upload:
        future = pool.submit(upload_queued)
        next_upload = now + 60
  finally:
    upload_allowed = False
    capture_loop.close()
    pool.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
  main()
