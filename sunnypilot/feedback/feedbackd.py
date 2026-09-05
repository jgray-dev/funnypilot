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
  # Include timestamps/validity so stale samples cannot masquerade as fresh data.
  row['valid'] = {s:bool(sm.valid[s] and sm.alive[s]) for s in
                  ('carState','controlsState','longitudinalPlan','selfdriveState','liveCalibration')}
  return row


def main():
  import cereal.messaging as messaging
  from cereal import log
  from openpilot.common.params import Params
  from openpilot.system.hardware.hw import Paths
  from openpilot.common.swaglog import cloudlog

  os.nice(10)
  params = Params()
  capture = Capture(log_root=Paths.log_root(), identity=identity(Path(__file__).resolve().parents[2]))
  sm = messaging.SubMaster(['carState','controlsState','longitudinalPlan','deviceState','selfdriveState',
                           'liveTorqueParameters','carControl','radarState','longitudinalPlanSP','liveCalibration'], poll='carState')
  sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
  try:
    os.unlink(P.SOCKET)
  except FileNotFoundError:
    pass
  sock.bind(P.SOCKET)
  os.chmod(P.SOCKET, 0o600)
  sock.setblocking(False)
  upload_allowed = False
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='feedback-upload')
  future = None
  last_sample = next_upload = 0.0

  def upload_queued():
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
      if sm.updated['carState'] and now - last_sample >= .01:
        try:
          capture.sample(snapshot(sm, now))
        except Exception:
          cloudlog.exception('feedback sample unavailable')
        last_sample = now
      # Bound work per tick even if a broken client floods the socket.
      for _ in range(8):
        try:
          raw = sock.recv(2048)
        except BlockingIOError:
          break
        cmd = {}
        try:
          cmd = json.loads(raw)
          route = params.get('CurrentRoute') or ''
          if isinstance(route, bytes):
            route = route.decode()
          capture.report(cmd, route, now)
        except Exception as e:
          P.atomic_json(P.STATUS, {'id':cmd.get('id') if isinstance(cmd, dict) else '', 'saved':False,
                                   'message':str(e)[:120], 't':now})
      capture.finish(now)
      if future is not None and future.done():
        try:
          future.result()
        except Exception:
          cloudlog.exception('feedback upload worker failed')
        future = None
      if upload_allowed and future is None and now >= next_upload:
        future = pool.submit(upload_queued)
        next_upload = now + 60
  finally:
    sock.close()
    pool.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
  main()
