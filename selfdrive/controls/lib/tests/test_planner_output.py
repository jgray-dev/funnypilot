"""Real planner update/publish wiring around a prescribed solver trajectory.

These are integration tests of output shaping and lifecycle, not MPC or
vehicle-dynamics simulations. IPC, the solver, and optional speed governors
are isolated; controller arithmetic and capnp publication run unchanged.
"""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest
from cereal import car, log

from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.modeld.constants import ModelConstants

ROOT = Path(__file__).resolve().parents[4]
DT = 0.05


class PrescribedMpc:
  def __init__(self, dt):
    self.source = log.LongitudinalPlan.LongitudinalPlanSource.cruise
    self.crash_cnt = 0
    self.solve_time = 0.001
    self.command = 1.0

  def set_weights(self, *args, **kwargs):
    pass

  def set_cur_state(self, v, a):
    self.v0 = v

  def update(self, *args, **kwargs):
    t = np.array(ModelConstants.T_IDXS)
    self.v_solution = self.v0 + self.command * t
    self.a_solution = np.full(len(t), self.command)
    self.j_solution = np.zeros(len(t) - 1)


class Governors:
  mlsim = True

  def __init__(self, cp, cp_sp, mpc):
    self.sla = NS(gas_gate_active=False)
    self._scc_map_v2 = NS(gas_gating_active=False)

  def update(self, sm):
    pass

  def get_mpc_mode(self):
    return None

  def update_targets(self, sm, v, a, cruise):
    return cruise, a

  def publish_longitudinal_plan_sp(self, sm, pm):
    pass


class Signals(dict):
  def all_checks(self, service_list):
    return True


@pytest.fixture
def planner(monkeypatch):
  def bind(name, **attrs):
    module = ModuleType(name)
    module.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, module)
    return module

  def new_message(kind):
    event = log.Event.new_message(logMonoTime=20_050_000_000)
    event.init(kind)
    return event

  import cereal
  messaging = bind('cereal.messaging', new_message=new_message)
  monkeypatch.setattr(cereal, 'messaging', messaging, raising=False)
  bind('openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc',
       LongitudinalMpc=PrescribedMpc, LongitudinalPlanSource=log.LongitudinalPlan.LongitudinalPlanSource,
       T_IDXS=np.array(ModelConstants.T_IDXS), get_T_FOLLOW=lambda *args, **kwargs: 1.5, STOP_DISTANCE=7.5)
  bind('openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner', LongitudinalPlannerSP=Governors)
  bind('openpilot.sunnypilot.selfdrive.car.cruise_ext', VCruiseHelperSP=object)
  # Load cruise constants from their actual source; isolate its IO base class.
  for path in ('selfdrive/car/cruise.py', 'selfdrive/controls/lib/longitudinal_planner.py'):
    name = 'openpilot.' + path[:-3].replace('/', '.')
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)

  cp = NS(openpilotLongitudinalControl=True, steerRatio=13.0, wheelbase=2.8,
          longitudinalActuatorDelay=0.15, vEgoStopping=0.3)
  p = module.LongitudinalPlanner(cp, None, init_v=25.0)
  cs = car.CarState.new_message(vEgo=25.0, vCruise=120.0)
  model = log.ModelDataV2.new_message()
  model.position.x = (25.0 * np.array(ModelConstants.T_IDXS)).tolist()
  model.velocity.x = [25.0] * ModelConstants.IDX_N
  model.acceleration.x = [0.0] * ModelConstants.IDX_N
  model.orientationRate.z = [0.0] * ModelConstants.IDX_N
  sm = Signals(carState=cs, controlsState=NS(longControlState=LongCtrlState.pid, forceDecel=False),
               selfdriveState=NS(experimentalMode=False, enabled=True, personality=log.LongitudinalPersonality.standard),
               carControl=NS(orientationNED=[0.0, 0.0, 0.0]), modelV2=model,
               liveParameters=NS(angleOffsetDeg=0.0), radarState=log.RadarState.new_message())
  sm.logMonoTime = {'modelV2': 20_000_000_000}
  return p, sm


def test_shaper_tracks_published_accel_through_coast_gate(planner):
  p, sm = planner
  p.sla.gas_gate_active = True
  for _ in range(80):
    p.update(sm)
  assert p.output_a_target == pytest.approx(-0.3)
  assert p.shaper.a == p.output_a_target
  before = p.output_a_target
  p.sla.gas_gate_active = False
  p.update(sm)
  assert 0 < p.output_a_target - before <= 1.8 * DT + 1e-9
  assert p.shaper.a == p.output_a_target


@pytest.mark.parametrize('experimental', [False, True])
def test_braking_reaches_published_plan_in_one_update(planner, experimental):
  p, sm = planner
  for _ in range(30):
    p.update(sm)
  assert p.output_a_target > 0
  sm['selfdriveState'].experimentalMode = experimental
  sm['modelV2'].action.desiredAcceleration = -1.5
  p.mpc.command = -3.0
  p.update(sm)
  messages = {}
  p.publish(sm, NS(send=lambda name, message: messages.update({name: message})))
  assert messages['longitudinalPlan'].longitudinalPlan.aTarget == pytest.approx(-3.0)


def test_processing_delay_uses_seconds(planner):
  p, sm = planner
  messages = {}
  p.publish(sm, NS(send=lambda name, message: messages.update({name: message})))
  assert messages['longitudinalPlan'].longitudinalPlan.processingDelay == pytest.approx(0.05)


def test_disengaged_planner_does_not_accumulate_stop_evidence(planner):
  p, sm = planner
  lead = sm['radarState'].leadOne
  lead.status, lead.dRel, lead.vLead, lead.aLeadK = True, 30.0, 15.0, -3.0
  for _ in range(20):
    p.update(sm)
  assert p.stop_gov.stopping_lead
  sm['controlsState'].longControlState = LongCtrlState.off
  for _ in range(20):
    p.update(sm)
    assert not p.stop_gov.stopping_lead
