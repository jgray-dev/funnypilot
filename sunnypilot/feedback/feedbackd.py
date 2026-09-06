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
from openpilot.sunnypilot.feedback.carstate_shm import CarStateTapReader
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


def snapshot(sm, car, now):
  # `car` is a fresh record of card's /dev/shm carState mirror (carstate_shm),
  # or None when card is not publishing. The recorder holds no carState
  # subscription: msgq allows 15 readers per service, normal C3X driving uses
  # 14 on carState, and the 3.7.1a subscription here was the 16th, which
  # evicted calibrationd and locationd from carState and blocked engagement.
  # Field names are unchanged so existing telemetry tooling keeps working.
  ctrl, lp = sm['controlsState'], sm['longitudinalPlan']
  car = car or {}
  row = {'t':now, 't_car':car.get('observed'), 'v':car.get('v_ego'), 'a':car.get('a_ego'),
         'angle':car.get('steering_angle_deg'), 'driver_torque':car.get('steering_torque'),
         'steering_pressed':car.get('steering_pressed'), 'brake_pressed':car.get('brake_pressed'),
         'gas_pressed':car.get('gas_pressed'),
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
  # carState is valid only from a fresh mirror record whose CAN was valid.
  row['valid'] = {s:bool(sm.valid[s] and sm.alive[s]) for s in
                  ('controlsState','longitudinalPlan','selfdriveState','liveCalibration','onroadEvents','onroadEventsSP')}
  row['valid']['carState'] = bool(car.get('can_valid', False))
  return row


RETRY_S = 5.0
UPLOAD_GATE_S = 2.0  # deviceState is 2 Hz; the parked-on-Wi-Fi gate keeps that latency


class UploadGate:
  """Parked on Wi-Fi, without a deviceState subscription.

  deviceState is another full msgq service in normal driving (loggerd, selfdrived,
  modeld, ui, manager, statsd, four athenad upload workers, sunnylink, pandad),
  so the recorder asks the sources deviceState is built from: manager's IsOnroad
  param and the hardware's own network type, at most every UPLOAD_GATE_S.
  Any failure reads as "not allowed"; a wrong answer can only delay an upload.
  """

  def __init__(self, params, hardware, wifi):
    self.params, self.hardware, self.wifi = params, hardware, wifi
    self.allowed = False
    self.next_check = 0.0

  def update(self, now):
    if now >= self.next_check:
      self.next_check = now + UPLOAD_GATE_S
      try:
        self.allowed = bool(not self.params.get_bool('IsOnroad') and self.hardware.get_network_type() == self.wifi)
      except Exception:
        self.allowed = False
    return self.allowed


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
  from openpilot.system.hardware import HARDWARE
  from openpilot.system.hardware.hw import Paths
  from openpilot.common.swaglog import cloudlog

  try:
    os.nice(10)
  except OSError:
    cloudlog.exception('feedback scheduling priority unavailable')
  params = Params()
  capture_loop = CaptureLoop(Paths.log_root(), identity(Path(__file__).resolve().parents[2]), cloudlog)
  # NO carState AND NO deviceState READER HERE — both services are at msgq's
  # 15-reader limit in normal C3X driving (see carstate_shm.py and
  # sunnypilot/feedback/tests/test_reader_budget.py before adding any service).
  # carState scalars arrive through card's /dev/shm mirror; the 100 Hz cadence
  # comes from controlsState, which has six readers.
  sm = messaging.SubMaster(['controlsState','longitudinalPlan','selfdriveState',
                           'liveTorqueParameters','carControl','radarState','longitudinalPlanSP','liveCalibration',
                           'onroadEvents','onroadEventsSP'], poll='controlsState')
  tap = CarStateTapReader()
  gate = UploadGate(params, HARDWARE, log.DeviceState.NetworkType.wifi)
  upload_allowed = False
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='feedback-upload')
  future = None
  next_upload = 0.0
  next_motion = 0.0

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
      car = tap.poll(now)
      if now >= next_motion:
        P.publish_motion(tap.latest, now)
        next_motion = now + 0.2
      upload_allowed = gate.update(now)
      # Sample whenever the car is publishing: a fresh mirror record, or a
      # controlsState frame (its carState fields are then null, visibly).
      sample = (lambda now=now, car=car: snapshot(sm, car, now)) if (car is not None or sm.updated['controlsState']) else None
      capture_loop.tick(now, sample, lambda: params.get('CurrentRoute'))
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
