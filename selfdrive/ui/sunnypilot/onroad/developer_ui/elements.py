"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import time
import pyray as rl
from dataclasses import dataclass

from cereal import log
from openpilot.common.constants import CV


from openpilot.system.ui.lib.text_measure import measure_text_cached


@dataclass
class UiElement:
  value: str
  label: str
  unit: str
  color: rl.Color
  val_text: str = ""
  label_text: str = ""
  unit_text: str = ""
  val_width: float = 0.0
  label_width: float = 0.0
  unit_width: float = 0.0
  total_width: float = 0.0

  def measure(self, font, font_size: int):
    self.label_text = f"{self.label} "
    self.val_text = self.value
    self.unit_text = f" {self.unit}" if self.unit else ""

    self.label_width = measure_text_cached(font, self.label_text, font_size, 0).x
    self.val_width = measure_text_cached(font, self.val_text, font_size, 0).x
    self.unit_width = measure_text_cached(font, self.unit_text, font_size, 0).x if self.unit else 0

    self.total_width = self.label_width + self.val_width + self.unit_width


class LeadInfoElement:
  @staticmethod
  def get_lead_status(sm):
    lead_one = sm['radarState'].leadOne
    return lead_one.status, lead_one.dRel, lead_one.vRel

  @staticmethod
  def get_lead_color(lead_d_rel: float, lead_v_rel: float = 0.0, use_v_rel: bool = False) -> rl.Color:
    if use_v_rel:
      if lead_v_rel < -4.4704:
        return rl.RED
      elif lead_v_rel < 0:
        return rl.Color(255, 188, 0, 255)  # Orange
    else:
      if lead_d_rel < 5:
        return rl.RED
      elif lead_d_rel < 15:
        return rl.Color(255, 188, 0, 255)  # Orange
    return rl.WHITE


class LateralControlElement:
  @staticmethod
  def get_lat_color(lat_active: bool, steer_override: bool, angle_steers: float = 0.0,
                    check_angle: bool = False) -> rl.Color:
    color = rl.WHITE
    if lat_active:
      color = rl.Color(145, 155, 149, 255) if steer_override else rl.Color(0, 255, 0, 255)

    if check_angle and lat_active:
      if abs(angle_steers) > 180:
        color = rl.RED
      elif abs(angle_steers) > 90:
        color = rl.Color(255, 188, 0, 255)
      else:
        # Keep green/grey from above
        pass
    elif check_angle and not lat_active:
      if abs(angle_steers) > 180:
        color = rl.RED
      elif abs(angle_steers) > 90:
        color = rl.Color(255, 188, 0, 255)

    return color


class RelDistElement(LeadInfoElement):
  def __init__(self):
    self.unit = "m"

  def update(self, sm, is_metric: bool) -> UiElement:
    lead_status, lead_d_rel, _ = self.get_lead_status(sm)
    value = f"{lead_d_rel:.0f}" if lead_status else "-"
    color = self.get_lead_color(lead_d_rel) if lead_status else rl.WHITE
    return UiElement(value, "REL DIST", self.unit, color)


class RelSpeedElement(LeadInfoElement):
  def __init__(self):
    self.unit = "km/h"

  def update(self, sm, is_metric: bool) -> UiElement:
    lead_status, _, lead_v_rel = self.get_lead_status(sm)

    self.unit = "km/h" if is_metric else "mph"

    conversion = CV.MS_TO_KPH if is_metric else CV.MS_TO_MPH
    value = f"{lead_v_rel * conversion:.0f}" if lead_status else "-"
    color = self.get_lead_color(0, lead_v_rel, use_v_rel=True) if lead_status else rl.WHITE

    return UiElement(value, "REL SPEED", self.unit, color)


class SteeringAngleElement(LateralControlElement):
  def __init__(self):
    self.unit = ""

  def update(self, sm, is_metric: bool) -> UiElement:
    car_state = sm['carState']
    angle_steers = car_state.steeringAngleDeg
    lat_active = sm['carControl'].latActive
    steer_override = car_state.steeringPressed

    value = f"{angle_steers:.1f}°"
    color = self.get_lat_color(lat_active, steer_override, angle_steers, check_angle=True)

    return UiElement(value, "REAL STEER", self.unit, color)


class DesiredSteeringAngleElement(LateralControlElement):
  def __init__(self):
    self.unit = ""

  def update(self, sm, is_metric: bool) -> UiElement:
    car_state = sm['carState']
    controls_state = sm['controlsState']
    lat_active = sm['carControl'].latActive
    angle_steers = car_state.steeringAngleDeg
    steer_angle_desired = controls_state.lateralControlState.angleState.steeringAngleDeg

    value = f"{steer_angle_desired:.1f}°" if lat_active else "-"

    color = rl.WHITE
    if lat_active:
      if abs(angle_steers) > 180:
        color = rl.RED
      elif abs(angle_steers) > 90:
        color = rl.Color(255, 188, 0, 255)
      else:
        color = rl.Color(0, 255, 0, 255)

    return UiElement(value, "DESIRED STEER", self.unit, color)


class ActualLateralAccelElement(LateralControlElement):
  def __init__(self):
    self.unit = "m/s^2"

  def update(self, sm, is_metric: bool) -> UiElement:
    controls_state = sm['controlsState']
    curvature = controls_state.curvature
    v_ego = sm['carState'].vEgo
    roll = sm['liveParameters'].roll if sm.valid['liveParameters'] else 0.0
    lat_active = sm['carControl'].latActive
    steer_override = sm['carState'].steeringPressed

    actual_lat_accel = (curvature * v_ego ** 2) - (roll * 9.81)
    value = f"{actual_lat_accel:.2f}"
    color = self.get_lat_color(lat_active, steer_override)

    return UiElement(value, "ACTUAL L.A.", self.unit, color)


class DesiredLateralAccelElement(LateralControlElement):
  def __init__(self):
    self.unit = "m/s^2"

  def update(self, sm, is_metric: bool) -> UiElement:
    controls_state = sm['controlsState']
    desired_curvature = controls_state.desiredCurvature
    v_ego = sm['carState'].vEgo
    roll = sm['liveParameters'].roll if sm.valid['liveParameters'] else 0.0
    lat_active = sm['carControl'].latActive
    steer_override = sm['carState'].steeringPressed

    desired_lat_accel = (desired_curvature * v_ego ** 2) - (roll * 9.81)
    value = f"{desired_lat_accel:.2f}" if lat_active else "-"
    color = self.get_lat_color(lat_active, steer_override)

    return UiElement(value, "DESIRED L.A.", self.unit, color)


class DesiredSteeringPIDElement(LateralControlElement):
  def __init__(self):
    self.unit = ""

  def update(self, sm, is_metric: bool) -> UiElement:
    car_state = sm['carState']
    controls_state = sm['controlsState']
    lat_active = sm['carControl'].latActive
    angle_steers = car_state.steeringAngleDeg
    steer_angle_desired = controls_state.lateralControlState.pidState.steeringAngleDesiredDeg

    value = f"{steer_angle_desired:.1f}°" if lat_active else "-"

    color = rl.WHITE
    if lat_active:
      if abs(angle_steers) > 180:
        color = rl.RED
      elif abs(angle_steers) > 90:
        color = rl.Color(255, 188, 0, 255)
      else:
        color = rl.Color(0, 255, 0, 255)

    return UiElement(value, "DESIRED STEER", self.unit, color)


class AEgoElement:
  def __init__(self):
    self.unit = "m/s^2"

  def update(self, sm, is_metric: bool) -> UiElement:
    a_ego = sm['carState'].aEgo
    value = f"{a_ego:.1f}"
    return UiElement(value, "ACC.", self.unit, rl.WHITE)


class FrictionCoefficientElement:
  def __init__(self):
    self.unit = ""

  def update(self, sm, is_metric: bool) -> UiElement:
    ltp = sm['liveTorqueParameters']
    friction_coef = ltp.frictionCoefficientFiltered
    live_valid = ltp.liveValid

    value = f"{friction_coef:.3f}"
    color = rl.Color(0, 255, 0, 255) if live_valid else rl.WHITE
    return UiElement(value, "FRIC.", self.unit, color)


class LatAccelFactorElement:
  def __init__(self):
    self.unit = ""

  def update(self, sm, is_metric: bool) -> UiElement:
    ltp = sm['liveTorqueParameters']
    lat_accel_factor = ltp.latAccelFactorFiltered
    live_valid = ltp.liveValid

    value = f"{lat_accel_factor:.3f}"
    color = rl.Color(0, 255, 0, 255) if live_valid else rl.WHITE
    return UiElement(value, "L.A.F.", self.unit, color)


def _read_lat_interp():
  """Reads controlsd's /dev/shm/lat_interp heartbeat. v3.3.8 format is
  "n,authority,pitch,limited": n = realized control-frames-per-model-frame,
  authority = min EPS-governor bound since the last model frame (1.0 = the
  hardware driver-torque clamp never engaged), pitch = max car-frame
  pitch-rate magnitude (deg/s) since the last model frame (UNVERIFIED
  weight-transfer/bump hypothesis), limited = fraction of control frames
  since the last model frame where the governor's bound actually clamped the
  request. Returns (n, authority, pitch, limited)."""
  try:
    with open('/dev/shm/lat_interp') as f:
      parts = f.read().strip().split(',')
      n = int(parts[0])
      auth = float(parts[1]) if len(parts) > 1 else 1.0
      pitch = float(parts[2]) if len(parts) > 2 else 0.0
      limited = float(parts[3]) if len(parts) > 3 else 0.0
      return n, auth, pitch, limited
  except Exception:
    return 0, 1.0, 0.0, 0.0


class _RollingExtreme:
  """Peak-hold over a short window so 100 Hz/20 Hz transients survive long
  enough to be read at a glance (same idea as the old v3.0.7 dCRV hold)."""

  def __init__(self, window_s: float, track_min: bool):
    self.window_s = window_s
    self.track_min = track_min
    self._samples: list[tuple[float, float]] = []

  def update(self, value: float) -> float:
    now = time.monotonic()
    self._samples.append((now, value))
    self._samples = [(t, v) for t, v in self._samples if now - t <= self.window_s]
    vals = [v for _, v in self._samples]
    return min(vals) if self.track_min else max(vals)


class EpsLimitElement:
  # FunnyPilot v3.3.8: replaces INTERP (which had become a static "5"). Shows
  # the EPS torque governor's authority bound — the fraction of full steering
  # torque the hardware driver-torque clamp is currently willing to pass
  # (eps_limit.py). 100% green = clamp not engaged; anything less means the
  # carcontroller/panda limit is LIVE and stripping torque: the prime suspect
  # for the turn-in grab/loosen oscillation. 1 s min-hold so brief dips are
  # readable. Glance rule: behaving nicely => "100"; during an event, note
  # what this shows.
  def __init__(self):
    self.unit = "%"
    self._hold = _RollingExtreme(1.0, track_min=True)

  def update(self, sm, is_metric: bool) -> UiElement:
    lat_active = sm['carControl'].latActive
    _, auth, _, _ = _read_lat_interp()
    held = self._hold.update(auth)
    if not lat_active:
      return UiElement("-", "EPS", self.unit, rl.WHITE)
    if held >= 0.99:
      color = rl.Color(0, 255, 0, 255)    # full authority
    elif held >= 0.60:
      color = rl.Color(255, 165, 0, 255)  # clamp engaged, moderate
    else:
      color = rl.Color(255, 0, 0, 255)    # deep clamp — the oscillation regime
    return UiElement(f"{held * 100:.0f}", "EPS", self.unit, color)


class DriverTorqueElement:
  # FunnyPilot v3.3.8: raw torsion-bar reading (CS.steeringTorque), 1 s
  # max-hold. This is the sensor that drives the hardware driver-torque clamp:
  # green < 50 (below the clamp threshold — hardware untouched), orange
  # 50-149 (CLAMP BAND: authority is being stripped, yet steeringPressed
  # can't see it), red >= 150 (steeringPressed band). Hands off, a green
  # value during an oscillation event FALSIFIES the clamp hypothesis; orange+
  # confirms the band was reachable. Pairs with triage "dtx".
  def __init__(self):
    self.unit = ""
    self._hold = _RollingExtreme(1.0, track_min=False)

  def update(self, sm, is_metric: bool) -> UiElement:
    tq = abs(sm['carState'].steeringTorque)
    held = self._hold.update(tq)
    if held < 50:
      color = rl.Color(0, 255, 0, 255)
    elif held < 150:
      color = rl.Color(255, 165, 0, 255)
    else:
      color = rl.Color(255, 0, 0, 255)
    return UiElement(f"{held:.0f}", "TBAR", self.unit, color)


class TorqueLimitActiveElement:
  # FunnyPilot v3.3.8: replaces L.S. (lead speed — redundant with the REL
  # SPEED/REL DIST readouts elsewhere). EPS shows the current AUTHORITY
  # CEILING the hardware driver-torque clamp allows; that ceiling can sit
  # below 100% without ever actually being hit by the request. This shows
  # whether the clamp is ACTUALLY biting — the fraction of the last second's
  # control frames where the governor's bound clamped what latcontrol wanted
  # to send (`EpsTorqueGovernor.driver_limited`). 0% green = the model's
  # desired torque is passing through unmodified; higher means the hardware
  # is actively stripping torque from the request, which is the direct,
  # real-time signal for "are we fighting the clamp right now."
  def __init__(self):
    self.unit = "%"
    self._hold = _RollingExtreme(1.0, track_min=False)

  def update(self, sm, is_metric: bool) -> UiElement:
    lat_active = sm['carControl'].latActive
    _, _, _, limited = _read_lat_interp()
    held = self._hold.update(limited)
    if not lat_active:
      return UiElement("-", "LIM", self.unit, rl.WHITE)
    if held <= 0.01:
      color = rl.Color(0, 255, 0, 255)    # request passing through unmodified
    elif held < 0.50:
      color = rl.Color(255, 165, 0, 255)  # intermittently clamped
    else:
      color = rl.Color(255, 0, 0, 255)    # persistently clamped
    return UiElement(f"{held * 100:.0f}", "LIM", self.unit, color)


class SuspensionBumpElement:
  # FunnyPilot v3.3.8: UNVERIFIED weight-transfer/bump hypothesis. User
  # observed the turn-in oscillation crossing railroad tracks mid-corner
  # with EPS pinned at 100% (i.e. NOT the driver-torque clamp) — the
  # candidate mechanism is front-axle weight transfer over the bump (grip
  # loss / suspension rebound) rather than a torque-request problem at all.
  # This reads the car's own IMU (car-frame Y-axis angular rate, ~pitch
  # rate — a bump should show a coherent spike as the nose dips/rebounds),
  # already computed every frame in controlsd as calibrated_pose; no new
  # subscription. 1 s max-hold, same pattern as TBAR. DELIBERATELY
  # uncolored (no claimed-confident thresholds yet — this is a first look,
  # not a calibrated alarm): read the raw number and correlate it against
  # the felt oscillation and the triage "pit" field before trusting it.
  def __init__(self):
    self.unit = "°/s"
    self._hold = _RollingExtreme(1.0, track_min=False)

  def update(self, sm, is_metric: bool) -> UiElement:
    _, _, pitch, _ = _read_lat_interp()
    held = self._hold.update(pitch)
    return UiElement(f"{held:.0f}", "BUMP", self.unit, rl.WHITE)


class LagdElement:
  # FunnyPilot v3.3.3st: real-time readout of the live-learned steer
  # actuator delay (locationd/lagd.py's liveDelay.lateralDelay) so the
  # value the Models page's "Live Learning Steer Delay" toggle is riding
  # stays visible on the road, without opening settings.
  def __init__(self):
    self.unit = "s"

  def update(self, sm, is_metric: bool) -> UiElement:
    live_delay = sm['liveDelay']
    lag = live_delay.lateralDelay
    status = live_delay.status

    value = f"{lag:.3f}"
    if status == log.LiveDelayData.Status.estimated:
      color = rl.Color(0, 255, 0, 255)
    elif status == log.LiveDelayData.Status.invalid:
      color = rl.RED
    else:
      color = rl.WHITE

    return UiElement(value, "LAGD", self.unit, color)


class SteeringTorqueEpsElement:
  def __init__(self):
    self.unit = "N·dm"

  def update(self, sm, is_metric: bool) -> UiElement:
    steering_torque_eps = sm['carState'].steeringTorqueEps
    value = f"{abs(steering_torque_eps):.1f}"
    return UiElement(value, "E.T.", self.unit, rl.WHITE)


class GpsInfoElement:
  @staticmethod
  def get_gps_data(sm):
    if sm.valid['gpsLocationExternal']:
      return sm['gpsLocationExternal'], True
    elif sm.valid['gpsLocation']:
      return sm['gpsLocation'], True
    return None, False


class BearingDegElement(GpsInfoElement):
  def __init__(self):
    self.unit = ""

  def update(self, sm, is_metric: bool) -> UiElement:
    gps_data, valid = self.get_gps_data(sm)
    if not valid:
      return UiElement("OFF | -", "B.D.", self.unit, rl.WHITE)

    bearing_accuracy_deg = gps_data.bearingAccuracyDeg
    bearing_deg = gps_data.bearingDeg

    if bearing_accuracy_deg != 180.0:
      value = f"{bearing_deg:.0f}°"
      if (337.5 <= bearing_deg <= 360) or (0 <= bearing_deg <= 22.5):
        dir_value = "N"
      elif 22.5 < bearing_deg < 67.5:
        dir_value = "NE"
      elif 67.5 <= bearing_deg <= 112.5:
        dir_value = "E"
      elif 112.5 < bearing_deg < 157.5:
        dir_value = "SE"
      elif 157.5 <= bearing_deg <= 202.5:
        dir_value = "S"
      elif 202.5 < bearing_deg < 247.5:
        dir_value = "SW"
      elif 247.5 <= bearing_deg <= 292.5:
        dir_value = "W"
      else:  # 292.5 < bearing_deg < 337.5
        dir_value = "NW"
    else:
      value = "-"
      dir_value = "OFF"

    return UiElement(f"{dir_value} | {value}", "B.D.", self.unit, rl.WHITE)


class AltitudeElement(GpsInfoElement):
  def __init__(self):
    self.unit = "m"

  def update(self, sm, is_metric: bool) -> UiElement:
    gps_data, valid = self.get_gps_data(sm)

    gps_accuracy = 0.0
    altitude = 0.0

    if valid:
      altitude = gps_data.altitude
      if sm.valid['gpsLocationExternal']:
        gps_accuracy = gps_data.horizontalAccuracy
      else:
        gps_accuracy = 1.0  # Simulate valid for legacy check

    value = f"{altitude:.1f}" if gps_accuracy != 0.0 else "-"
    return UiElement(value, "ALT.", self.unit, rl.WHITE)
