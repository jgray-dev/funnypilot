"""Exercise the real steering controllers with IO and model inference isolated.

The PID, neural input construction, extension dispatch, handback, and torque
governor are production code. Only Params and the vehicle/model responses are
fixtures; no compiled IPC or physical vehicle is required.
"""
import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import pytest
from cereal import car, log

from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.sunnypilot.selfdrive.controls.lib.nnlc.helpers import MOCK_MODEL_PATH

ROOT = Path(__file__).resolve().parents[4]
DT = 0.01


@pytest.fixture
def controllers(monkeypatch):
  params = ModuleType('openpilot.common.params')
  params.Params = lambda: NS(get_bool=lambda key: False)
  monkeypatch.setitem(sys.modules, params.__name__, params)
  loaded = {}
  # Load private copies, with scoped import bindings, so fake Params never
  # leaks into another suite's normal module imports.
  for path in (
    'sunnypilot/selfdrive/controls/lib/nnlc/nnlc.py',
    'sunnypilot/selfdrive/controls/lib/latcontrol_torque_ext_override.py',
    'sunnypilot/selfdrive/controls/lib/latcontrol_torque_ext.py',
    'selfdrive/controls/lib/latcontrol_torque.py',
    'sunnypilot/selfdrive/controls/lib/latcontrol_torque_v0.py',
    'selfdrive/controls/lib/latcontrol_pid.py',
  ):
    name = 'openpilot.' + path[:-3].replace('/', '.')
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    loaded[Path(path).stem] = module
  return loaded


def make_torque(controllers, neural=False, legacy=False):
  cp = car.CarParams.new_message()
  cp.steerLimitTimer = 0.8
  cp.steerActuatorDelay = 0.2
  cp.lateralTuning.init('torque')
  cp.lateralTuning.torque.latAccelFactor = 2.75
  ci = NS(torque_from_lateral_accel=lambda: lambda a, p: a / p.latAccelFactor,
          lateral_accel_from_torque=lambda: lambda t, p: t * p.latAccelFactor,
          torque_from_lateral_accel_in_torque_space=lambda: lambda inputs, p, gravity_adjusted: inputs.lateral_acceleration / p.latAccelFactor)
  sp = NS(neuralNetworkLateralControl=NS(model=NS(path=MOCK_MODEL_PATH)))
  module = controllers['latcontrol_torque_v0' if legacy else 'latcontrol_torque']
  ctrl = module.LatControlTorque(cp.as_reader(), sp, ci, DT)
  ctrl.extension.enabled = neural
  ctrl.extension.has_nn_model = True
  # A deterministic inference response makes one PID integration step
  # independently calculable from the logged neural-space error.
  ctrl.extension.model.evaluate = lambda inputs: inputs[1] * 0.1
  ctrl.extension.model.friction_override = False
  model = log.ModelDataV2.new_message()
  model.orientation.x = [0.0] * ModelConstants.IDX_N
  model.orientation.y = [0.0] * ModelConstants.IDX_N
  model.acceleration.y = [0.1] * ModelConstants.IDX_N
  ctrl.extension.update_model_v2(model)
  cs = car.CarState.new_message(vEgo=20.0)
  params = log.LiveParametersData.new_message()
  vm = NS(calc_curvature=lambda angle, speed, roll: 0.0)
  return ctrl, cs, vm, params, model


def tick(ctrl, cs, vm, params, active=True):
  return ctrl.update(active, cs, vm, params, False, 0.00025, None, False, 0.2)


@pytest.mark.parametrize('legacy', [False, True])
def test_neural_pid_integrates_exactly_once(controllers, legacy):
  ctrl, cs, vm, params, _ = make_torque(controllers, neural=True, legacy=legacy)
  # Fill the delay buffer without integrating (driver holds the wheel).
  cs.steeringPressed = True
  for _ in range(25):
    tick(ctrl, cs, vm, params)
  cs.steeringPressed = False
  ctrl.pid.reset()
  _, _, state = tick(ctrl, cs, vm, params)
  assert abs(state.error) > 1e-5
  assert ctrl.pid.i == pytest.approx(ctrl.pid.k_i * DT * state.error, abs=1e-12)


def test_neural_handback_keeps_integrator_frozen(controllers):
  ctrl, cs, vm, params, _ = make_torque(controllers, neural=True)
  cs.steeringPressed = True
  for _ in range(100):
    tick(ctrl, cs, vm, params)
  cs.steeringPressed = False
  for _ in range(35):
    tick(ctrl, cs, vm, params)
  assert ctrl._handback.ramping and ctrl._handback.soft_integrator
  before = ctrl.pid.i
  _, _, state = tick(ctrl, cs, vm, params)
  assert abs(state.error) > 1e-5
  assert ctrl.pid.i == before


@pytest.mark.parametrize('legacy', [False, True])
def test_torque_disengagement_clears_pid(controllers, legacy):
  ctrl, cs, vm, params, _ = make_torque(controllers, legacy=legacy)
  for _ in range(100):
    tick(ctrl, cs, vm, params)
  assert ctrl.pid.i > 0
  tick(ctrl, cs, vm, params, active=False)
  assert ctrl.pid.i == 0


def test_angle_pid_disengagement_clears_pid(controllers):
  cp = car.CarParams.new_message(steerLimitTimer=0.8)
  cp.lateralTuning.init('pid')
  cp.lateralTuning.pid.kpBP = [0.0]
  cp.lateralTuning.pid.kpV = [0.1]
  cp.lateralTuning.pid.kiBP = [0.0]
  cp.lateralTuning.pid.kiV = [0.1]
  ci = NS(get_steer_feedforward_function=lambda: lambda angle, speed: 0.0)
  ctrl = controllers['latcontrol_pid'].LatControlPID(cp, None, ci, DT)
  cs = car.CarState.new_message(vEgo=20.0)
  vm = NS(get_steer_from_curvature=lambda curvature, speed, roll: 0.01)
  params = log.LiveParametersData.new_message()
  tick(ctrl, cs, vm, params)
  assert ctrl.pid.i > 0
  ctrl.reset()
  assert ctrl.pid.i == 0


@pytest.mark.parametrize('legacy', [False, True])
def test_neural_mode_switch_resets_units_and_limits(controllers, legacy):
  ctrl, cs, vm, params, model = make_torque(controllers, legacy=legacy)
  for _ in range(100):
    tick(ctrl, cs, vm, params)
  assert ctrl.pid.i > 0
  ctrl.extension.enabled = True
  cs.steeringPressed = True
  tick(ctrl, cs, vm, params)
  assert ctrl.pid.i == 0
  assert ctrl.pid.pos_limit == 1.0
  # Losing a usable model returns to acceleration-space control. A torque
  # integrator and +/-1 torque limits cannot be reused as m/s^2.
  ctrl.pid.i = 0.2
  ctrl.extension.update_model_v2(None)
  tick(ctrl, cs, vm, params)
  assert ctrl.pid.i == 0
  assert ctrl.pid.pos_limit == pytest.approx(2.75)
  ctrl.extension.update_model_v2(model)
  tick(ctrl, cs, vm, params)
  assert ctrl.pid.pos_limit == 1.0


@pytest.mark.parametrize('field', ['roll', 'pitch', 'accel'])
@pytest.mark.parametrize('bad', ['short', 'nan'])
def test_partial_neural_plan_falls_back_without_crashing(controllers, field, bad):
  ctrl, cs, vm, params, model = make_torque(controllers, neural=True)
  values = [0.0] if bad == 'short' else [float('nan')] * ModelConstants.IDX_N
  if field == 'roll':
    model.orientation.x = values
  elif field == 'pitch':
    model.orientation.y = values
  else:
    model.acceleration.y = values
  ctrl.extension.update_model_v2(model)
  assert not ctrl.extension._nnlc_enabled
  output, _, _ = tick(ctrl, cs, vm, params)
  assert -1.0 <= output <= 1.0


def test_real_k5_neural_model_and_history_reset(controllers):
  ctrl, cs, vm, params, _ = make_torque(controllers, neural=True)
  model_type = controllers['nnlc'].NNTorqueModel
  ctrl.extension.model = model_type(str(Path(MOCK_MODEL_PATH).with_name('KIA_K5_2021.json')))
  for _ in range(50):
    output, _, state = tick(ctrl, cs, vm, params)
    assert math.isfinite(state.error) and math.isfinite(state.i)
    assert -1.0 <= output <= 1.0
  assert len(ctrl.extension.roll_deque) > 0
  tick(ctrl, cs, vm, params, active=False)
  assert not ctrl.extension.roll_deque
  assert not ctrl.extension.lateral_accel_desired_deque
  assert ctrl.pid.i == 0.0
