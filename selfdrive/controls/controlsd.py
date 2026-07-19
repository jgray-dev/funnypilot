#!/usr/bin/env python3
import math
import time
from numbers import Number

from cereal import car, log
import cereal.messaging as messaging
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process, DT_CTRL, DT_MDL, Priority, Ratekeeper
from openpilot.common.swaglog import cloudlog

from opendbc.car.car_helpers import interfaces
from opendbc.car.vehicle_model import VehicleModel
from openpilot.selfdrive.controls.lib.drive_helpers import clip_curvature, get_curvature_from_plan, MAX_CURVATURE
from openpilot.selfdrive.controls.lib.lat_smooth import LatSmoother
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.triage_recorder import TriageRecorder, LatInterpMonitor
from openpilot.selfdrive.controls.lib.latcontrol import LatControl
from openpilot.selfdrive.controls.lib.latcontrol_pid import LatControlPID
from openpilot.selfdrive.controls.lib.latcontrol_angle import LatControlAngle, STEER_ANGLE_SATURATION_THRESHOLD
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.selfdrive.controls.lib.longcontrol import LongControl
from openpilot.selfdrive.locationd.helpers import PoseCalibrator, Pose

from openpilot.sunnypilot.selfdrive.controls.controlsd_ext import ControlsExt

State = log.SelfdriveState.OpenpilotState
LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection

ACTUATOR_FIELDS = tuple(car.CarControl.Actuators.schema.fields.keys())

class Controls(ControlsExt):
  def __init__(self) -> None:
    self.params = Params()
    cloudlog.info("controlsd is waiting for CarParams")
    self.CP = messaging.log_from_bytes(self.params.get("CarParams", block=True), car.CarParams)
    cloudlog.info("controlsd got CarParams")

    # Initialize sunnypilot controlsd extension and base model state
    ControlsExt.__init__(self, self.CP, self.params)

    self.CI = interfaces[self.CP.carFingerprint](self.CP, self.CP_SP)

    self.sm = messaging.SubMaster(['liveDelay', 'liveParameters', 'liveTorqueParameters', 'modelV2', 'selfdriveState',
                                   'liveCalibration', 'livePose', 'longitudinalPlan', 'carState', 'carOutput',
                                   'driverMonitoringState', 'onroadEvents', 'driverAssistance', 'liveDelay'] + self.sm_services_ext,
                                  poll='selfdriveState')
    self.pm = messaging.PubMaster(['carControl', 'controlsState'] + self.pm_services_ext)

    self.steer_limited_by_safety = False
    self.curvature = 0.0
    self.desired_curvature = 0.0
    # FunnyPilot v3.3.6: the validated knot interpolation (3.1.0e delta/5
    # timing), minimal and time-anchored, now shaped as a C1 monotone spline
    # between knots (SPLINE default in lat_smooth.py; the knot values/times and
    # every safety invariant are unchanged from the validated v3.2.10 scheme).
    self.lat_smooth = LatSmoother()

    # FunnyPilot v3.2.7: triage flight recorder — 1 Hz onroad evidence for the
    # recurring "smoothing feels off after sitting parked" report. Viewable and
    # copyable from the web UI (Logs button). Best-effort: never breaks controls.
    self.triage = LatInterpMonitor(TriageRecorder("lat_interp"))

    self.pose_calibrator = PoseCalibrator()
    self.calibrated_pose: Pose | None = None

    self.LoC = LongControl(self.CP, self.CP_SP)
    self.VM = VehicleModel(self.CP)
    self.LaC: LatControl
    if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
      self.LaC = LatControlAngle(self.CP, self.CP_SP, self.CI, DT_CTRL)
    elif self.CP.lateralTuning.which() == 'pid':
      self.LaC = LatControlPID(self.CP, self.CP_SP, self.CI, DT_CTRL)
    elif self.CP.lateralTuning.which() == 'torque':
      self.LaC = LatControlTorque(self.CP, self.CP_SP, self.CI, DT_CTRL)

    self.LaC = ControlsExt.initialize_lateral_control(self, self.LaC, self.CI, DT_CTRL)

  def update(self):
    self.sm.update(15)
    if self.sm.updated["liveCalibration"]:
      self.pose_calibrator.feed_live_calib(self.sm['liveCalibration'])
    if self.sm.updated["livePose"]:
      device_pose = Pose.from_live_pose(self.sm['livePose'])
      self.calibrated_pose = self.pose_calibrator.build_calibrated_pose(device_pose)

  def state_control(self):
    CS = self.sm['carState']

    # Update VehicleModel
    lp = self.sm['liveParameters']
    x = max(lp.stiffnessFactor, 0.1)
    sr = max(lp.steerRatio, 0.1)
    self.VM.update_params(x, sr)

    steer_angle_without_offset = math.radians(CS.steeringAngleDeg - lp.angleOffsetDeg)
    self.curvature = -self.VM.calc_curvature(steer_angle_without_offset, CS.vEgo, lp.roll)

    # Update Torque Params
    if self.CP.lateralTuning.which() == 'torque':
      torque_params = self.sm['liveTorqueParameters']
      if self.sm.all_checks(['liveTorqueParameters']) and torque_params.useParams:
        self.LaC.update_live_torque_params(torque_params.latAccelFactorFiltered, torque_params.latAccelOffsetFiltered,
                                           torque_params.frictionCoefficientFiltered)

        self.LaC.extension.update_limits()

      self.LaC.extension.update_model_v2(self.sm['modelV2'])

      self.LaC.extension.update_lateral_lag(self.lat_delay)

    long_plan = self.sm['longitudinalPlan']
    model_v2 = self.sm['modelV2']

    CC = car.CarControl.new_message()
    CC.enabled = self.sm['selfdriveState'].enabled

    # Check which actuators can be enabled
    standstill = abs(CS.vEgo) <= max(self.CP.minSteerSpeed, 0.3) or CS.standstill

    # Get which state to use for active lateral control
    _lat_active = self.get_lat_active(self.sm)

    CC.latActive = _lat_active and not CS.steerFaultTemporary and not CS.steerFaultPermanent and \
                   (not standstill or self.CP.steerAtStandstill)
    CC.longActive = CC.enabled and not any(e.overrideLongitudinal for e in self.sm['onroadEvents']) and \
                    (self.CP.openpilotLongitudinalControl or not self.CP_SP.pcmCruiseSpeed)

    actuators = CC.actuators
    actuators.longControlState = self.LoC.long_control_state

    # Enable blinkers while lane changing
    lane_change_active = model_v2.meta.laneChangeState != LaneChangeState.off
    if lane_change_active:
      CC.leftBlinker = model_v2.meta.laneChangeDirection == LaneChangeDirection.left
      CC.rightBlinker = model_v2.meta.laneChangeDirection == LaneChangeDirection.right

    if not CC.latActive:
      self.LaC.reset()
    if not CC.longActive:
      self.LoC.reset()

    # accel PID loop
    pid_accel_limits = self.CI.get_pid_accel_limits(self.CP, self.CP_SP, CS.vEgo, CS.vCruise * CV.KPH_TO_MS)
    actuators.accel = float(self.LoC.update(CC.longActive, CS, long_plan.aTarget, long_plan.shouldStop, pid_accel_limits))

    # Steering PID loop and lateral MPC
    # FunnyPilot v3.2.12: the modeld-side smoothing is TOTAL-PRESERVING — the
    # action is sampled earlier by its EMA time constant, so the command's
    # effective timing equals the full configured delay. The setpoint buffer
    # therefore aligns on lateralDelay directly, with no smoothing constant
    # added (the previous +LAT_SMOOTH_SECONDS import also silently ignored the
    # per-bundle override when modeld_v2 was the active daemon).
    lat_delay = self.sm["liveDelay"].lateralDelay

    # FunnyPilot v3.3.6: interpolate the model's 20 Hz desired curvature across
    # the control frames on the validated delta/5 TIMING (see lat_smooth.py for
    # the 3.2.9e and 3.3.2 post-mortems — both still binding), with the
    # in-period SHAPE upgraded to a C1 monotone spline. The exit slope of each
    # 50 ms segment is aimed at where the model's own published plan says the
    # desire goes one model step past the action horizon — a pure read of the
    # plan inside the lagd delay window, not a filter: every knot value is
    # still reached exactly on the validated schedule, so maneuver onset
    # cannot creep early (the v3.2.12 EMA failure) or arrive late. prev is
    # always the last OUTPUT so the command is continuous at any cadence;
    # time-anchoring to the fixed model period means nothing counts frames and
    # nothing can silently stall.
    if not CC.latActive:
      self.lat_smooth.reset(self.curvature)
      new_desired_curvature = self.curvature
    else:
      next_est = self._model_lookahead_curv(model_v2, lat_delay, CS.vEgo) if self.sm.updated['modelV2'] else None
      new_desired_curvature = self.lat_smooth.update(model_v2.action.desiredCurvature,
                                                     self.sm.updated['modelV2'], time.monotonic(), next_est)
    # Dev-UI INTERP indicator: realized control-frames-per-model-frame (5 =
    # healthy cadence), or 0 when paused. Written at the 20 Hz model rate.
    if self.sm.updated['modelV2']:
      try:
        n = round(self.lat_smooth.health_frames) if CC.latActive else 0
        with open('/dev/shm/lat_interp', 'w') as _f:
          _f.write(str(n))
      except Exception:
        pass
    self.desired_curvature, curvature_limited = clip_curvature(CS.vEgo, self.desired_curvature, new_desired_curvature, lp.roll)

    actuators.curvature = self.desired_curvature
    steer, steeringAngleDeg, lac_log = self.LaC.update(CC.latActive, CS, self.VM, lp,
                                                       self.steer_limited_by_safety, self.desired_curvature,
                                                       self.calibrated_pose, curvature_limited, lat_delay)
    actuators.torque = float(steer)
    actuators.steeringAngleDeg = float(steeringAngleDeg)

    # FunnyPilot v3.2.7: 1 Hz triage record — interp health, active fractions,
    # long targets, plus lateral-oscillation evidence (steeringPressed edges,
    # driver-override scale, saturation) for the "bite then loosen" report;
    # live-tuning context every 10 s (drift is hypothesis C for "feels off").
    self.triage.sample(time.monotonic(), CC.latActive, CC.longActive, CS.vEgo,
                       self.lat_smooth.health_frames, lane_change_active, curvature_limited,
                       long_plan.aTarget, actuators.accel,
                       steering_pressed=CS.steeringPressed,
                       override_scale=getattr(self.LaC, '_override_scale', 1.0),
                       saturated=bool(getattr(lac_log, 'saturated', False)),
                       steer_limited=self.steer_limited_by_safety,
                       torque=actuators.torque,
                       eps_authority=getattr(getattr(self.LaC, '_eps_governor', None), 'authority', 1.0),
                       context_fn=lambda: {
                         "laf": round(float(self.sm['liveTorqueParameters'].latAccelFactorFiltered), 3),
                         "fric": round(float(self.sm['liveTorqueParameters'].frictionCoefficientFiltered), 4),
                         "aOff": round(float(lp.angleOffsetDeg), 3),
                         "stiff": round(float(lp.stiffnessFactor), 3),
                         "latDelay": round(float(self.sm['liveDelay'].lateralDelay), 3),
                         # measured EPS+chassis delay estimate vs. the (possibly
                         # artificially inflated) delay actually in use — if
                         # est << used, we are steering systematically early
                         "latDelayEst": round(float(self.sm['liveDelay'].lateralDelayEstimate), 3),
                       })
    # Ensure no NaNs/Infs
    for p in ACTUATOR_FIELDS:
      attr = getattr(actuators, p)
      if not isinstance(attr, Number):
        continue

      if not math.isfinite(attr):
        cloudlog.error(f"actuators.{p} not finite {actuators.to_dict()}")
        setattr(actuators, p, 0.0)

    return CC, lac_log

  def _model_lookahead_curv(self, model_v2, lat_delay, vego):
    # FunnyPilot v3.3.6: estimate where the model's desired curvature is heading
    # one model step past the action horizon, used ONLY to aim the exit slope of
    # the in-period spline (lat_smooth.py). The model's action.desiredCurvature
    # is its plan curvature at ~(lat_delay + DT_MDL); we sample the same
    # published plan one step further. Pure read of an already-computed
    # trajectory — free compute inside the lagd delay window. Returns None on
    # any doubt (the spline then falls back to the plain validated secant), so
    # this can never inject an out-of-range curvature or shift a knot.
    try:
      yaws = model_v2.orientation.z
      yaw_rates = model_v2.orientationRate.z
      if len(yaws) < len(ModelConstants.T_IDXS) or len(yaw_rates) < 1:
        return None
      c = float(get_curvature_from_plan(yaws, yaw_rates, ModelConstants.T_IDXS, vego, lat_delay + 2 * DT_MDL))
      return c if math.isfinite(c) and abs(c) <= MAX_CURVATURE else None
    except Exception:
      return None

  def publish(self, CC, lac_log):
    CS = self.sm['carState']

    # Orientation and angle rates can be useful for carcontroller
    # Only calibrated (car) frame is relevant for the carcontroller
    CC.currentCurvature = self.curvature
    if self.calibrated_pose is not None:
      CC.orientationNED = self.calibrated_pose.orientation.xyz.tolist()
      CC.angularVelocity = self.calibrated_pose.angular_velocity.xyz.tolist()

    CC.cruiseControl.override = CC.enabled and not CC.longActive and (self.CP.openpilotLongitudinalControl or not self.CP_SP.pcmCruiseSpeed)
    CC.cruiseControl.cancel = CS.cruiseState.enabled and (not CC.enabled or not self.CP.pcmCruise)
    CC.cruiseControl.resume = CC.enabled and CS.cruiseState.standstill and not self.sm['longitudinalPlan'].shouldStop

    hudControl = CC.hudControl
    hudControl.setSpeed = float(CS.vCruiseCluster * CV.KPH_TO_MS)
    hudControl.speedVisible = CC.enabled
    hudControl.lanesVisible = CC.enabled
    hudControl.leadVisible = self.sm['longitudinalPlan'].hasLead
    hudControl.leadDistanceBars = self.sm['selfdriveState'].personality.raw + 1
    hudControl.visualAlert = self.sm['selfdriveState'].alertHudVisual

    hudControl.rightLaneVisible = True
    hudControl.leftLaneVisible = True
    if self.sm.valid['driverAssistance']:
      hudControl.leftLaneDepart = self.sm['driverAssistance'].leftLaneDeparture
      hudControl.rightLaneDepart = self.sm['driverAssistance'].rightLaneDeparture

    if self.sm['selfdriveState'].active:
      CO = self.sm['carOutput']
      if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
        self.steer_limited_by_safety = abs(CC.actuators.steeringAngleDeg - CO.actuatorsOutput.steeringAngleDeg) > \
                                              STEER_ANGLE_SATURATION_THRESHOLD
      else:
        self.steer_limited_by_safety = abs(CC.actuators.torque - CO.actuatorsOutput.torque) > 1e-2

    # TODO: both controlsState and carControl valids should be set by
    #       sm.all_checks(), but this creates a circular dependency

    # controlsState
    dat = messaging.new_message('controlsState')
    dat.valid = CS.canValid
    cs = dat.controlsState

    cs.curvature = self.curvature
    cs.longitudinalPlanMonoTime = self.sm.logMonoTime['longitudinalPlan']
    cs.lateralPlanMonoTime = self.sm.logMonoTime['modelV2']
    cs.desiredCurvature = self.desired_curvature
    cs.longControlState = self.LoC.long_control_state
    cs.upAccelCmd = float(self.LoC.pid.p)
    cs.uiAccelCmd = float(self.LoC.pid.i)
    cs.ufAccelCmd = float(self.LoC.pid.f)
    cs.forceDecel = bool((self.sm['driverMonitoringState'].awarenessStatus < 0.) or
                         (self.sm['selfdriveState'].state == State.softDisabling))

    lat_tuning = self.CP.lateralTuning.which()
    if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
      cs.lateralControlState.angleState = lac_log
    elif lat_tuning == 'pid':
      cs.lateralControlState.pidState = lac_log
    elif lat_tuning == 'torque':
      cs.lateralControlState.torqueState = lac_log

    self.pm.send('controlsState', dat)

    # carControl
    cc_send = messaging.new_message('carControl')
    cc_send.valid = CS.canValid
    cc_send.carControl = CC
    self.pm.send('carControl', cc_send)

  def run(self):
    rk = Ratekeeper(100, print_delay_threshold=None)
    while True:
      self.update()
      CC, lac_log = self.state_control()
      self.publish(CC, lac_log)
      self.get_params_sp(self.sm)
      self.run_ext(self.sm, self.pm)
      rk.monitor_time()


def main():
  config_realtime_process(4, Priority.CTRL_HIGH)
  controls = Controls()
  controls.run()


if __name__ == "__main__":
  main()
