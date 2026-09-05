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
from openpilot.selfdrive.controls.lib.knot_filter import KnotFilter
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

    # FunnyPilot v3.4.9: MORE interpolation, without the v3.2.12 EMA's phase
    # cost. LatSmoother can only shape the path BETWEEN knots; the knot
    # sequence itself still delivers each rate change inside one 50 ms period,
    # which is the step that is felt in the car. KnotFilter damps the part of
    # each knot the model's own published plan did NOT predict — so a steady
    # turn-in passes through bit-unchanged while jitter is spread over several
    # model frames — and hard-caps the resulting command-vs-desire deviation at
    # 0.15 m/s^2 of lateral accel (millimetres of path). See knot_filter.py.
    self.knot_filter = KnotFilter()

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
      self.knot_filter.reset()
      new_desired_curvature = self.curvature
    else:
      new_knot = self.sm.updated['modelV2']
      next_est = self._model_lookahead_curv(model_v2, self._model_action_delay(lat_delay), CS.vEgo) if new_knot else None
      # v3.4.9: `next_est` is the plan one model step past the ACTION horizon,
      # i.e. a prediction of the NEXT frame's action. The spline has used it to
      # aim its exit slope since v3.3.6; KnotFilter now also uses the PREVIOUS
      # frame's copy of it as the process model that decides how much of this
      # knot is new information. Same free read of the plan inside the lagd
      # window, no filtering of past outputs, no shift of any knot time.
      model_curv = model_v2.action.desiredCurvature
      if new_knot:
        model_curv = self.knot_filter.update(model_curv, CS.vEgo)

      new_desired_curvature = self.lat_smooth.update(model_curv, new_knot, time.monotonic(), next_est)
      if new_knot:
        self.knot_filter.set_prediction(next_est)
    # Dev-UI heartbeat, written at the 20 Hz model rate. v3.3.8: now
    # "n,authority,pitch,limited" — n is the realized control-frames-per-
    # model-frame (kept for triage compat), authority is the MIN EPS-governor
    # bound since the last model frame (1.00 = the hardware driver-torque
    # clamp never engaged; see eps_limit.py), pitch is the MAX car-frame
    # pitch-rate magnitude (deg/s) since the last model frame — an UNVERIFIED
    # weight-transfer/bump hypothesis for the railroad-track oscillation
    # (user observed it with EPS pinned at 100%, i.e. NOT the driver-torque
    # clamp) — read from the same calibrated IMU pose already computed each
    # frame for carControl's orientationNED/angularVelocity, no new
    # subscription. limited is the FRACTION of control frames since the last
    # model frame where the governor's bound was actually clamping the
    # request (`driver_limited`) — distinct from authority: the bound can sit
    # below 100% while never actually biting the request. The dev UI's
    # EPS/LIM/BUMP elements read this.
    eps_gov = getattr(self.LaC, '_eps_governor', None)
    eps_auth = getattr(eps_gov, 'authority', 1.0)
    self._eps_auth_min = min(getattr(self, '_eps_auth_min', 1.0), float(eps_auth))
    self._eps_limited_frames = getattr(self, '_eps_limited_frames', 0) + int(bool(getattr(eps_gov, 'driver_limited', False)))
    self._eps_total_frames = getattr(self, '_eps_total_frames', 0) + 1
    pitch_rate_deg = 0.0
    if self.calibrated_pose is not None:
      pitch_rate_deg = math.degrees(self.calibrated_pose.angular_velocity.pitch)
    self._pitch_max = max(getattr(self, '_pitch_max', 0.0), abs(pitch_rate_deg))
    if self.sm.updated['modelV2']:
      try:
        n = round(self.lat_smooth.health_frames) if CC.latActive else 0
        limited_frac = self._eps_limited_frames / max(self._eps_total_frames, 1)
        # v3.4.9 appends a FIFTH field, `dev`: the KnotFilter's current
        # command-vs-model-desire deviation in m/s^2 of lateral accel (hard
        # capped at DEV_MAX_LAT_ACCEL). Appended rather than inserted, and the
        # dev-UI reader indexes defensively, so older readers are unaffected.
        dev = self.knot_filter.deviation
        with open('/dev/shm/lat_interp', 'w') as _f:
          _f.write(f"{n},{self._eps_auth_min:.2f},{self._pitch_max:.1f},{limited_frac:.2f},{dev:.3f}")
      except Exception:
        pass
      self._eps_auth_min = 1.0
      self._pitch_max = 0.0
      self._eps_limited_frames = 0
      self._eps_total_frames = 0
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
                       driver_torque=CS.steeringTorque,
                       torque_out=self.sm['carOutput'].actuatorsOutput.torque,
                       pitch_rate_deg=pitch_rate_deg,
                       motion_scale=getattr(getattr(self.LaC, '_motion_credit', None), 'scale', 1.0),
                       motion_credit=getattr(getattr(self.LaC, '_motion_credit', None), 'credit', 0.0),
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

  def _model_action_delay(self, live_lat_delay):
    # FunnyPilot v3.4.9: the delay MODELD used to place the action, which is not
    # always liveDelay.lateralDelay — with the lagd toggle on, modeld_v2 samples
    # its plan at the cached lagd value (sunnypilot/livedelay/helpers.py) while
    # this file was reading the raw estimate. The lookahead below is defined as
    # "one model step past the ACTION horizon", so it has to be measured from
    # the same delay the action was, or the exit slope aims at the wrong point
    # and (v3.4.9) the knot prediction is biased. ControlsExt already computes
    # this via the SAME get_lat_delay helper for the torque controller; fall
    # back to the live estimate when it has not been read yet (first frames) or
    # for non-torque tunes where it is never set.
    d = getattr(self, 'lat_delay', None)
    return float(d) if isinstance(d, Number) and math.isfinite(d) and d > 0.0 else live_lat_delay

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
