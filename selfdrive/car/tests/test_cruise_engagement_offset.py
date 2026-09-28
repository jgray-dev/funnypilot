"""Exercise the real cruise helper with only persistent Params IO isolated."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import pytest
from cereal import car, custom
from openpilot.common.constants import CV

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def cruise(monkeypatch):
  params = ModuleType('openpilot.common.params')
  params.Params = lambda: NS(get_bool=lambda key: False, get=lambda *args, **kwargs: 1)
  monkeypatch.setitem(sys.modules, params.__name__, params)
  for relative in ('sunnypilot/selfdrive/controls/lib/speed_limit/helpers.py',
                   'sunnypilot/selfdrive/car/cruise_ext.py', 'selfdrive/car/cruise.py'):
    name = 'openpilot.' + relative[:-3].replace('/', '.')
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
  cp = car.CarParams.new_message(pcmCruise=False, openpilotLongitudinalControl=True)
  sp = custom.CarParamsSP.new_message(pcmCruiseSpeed=False)
  helper = module.VCruiseHelper(cp, sp)
  cs = car.CarState.new_message(vEgo=20.)
  cs.cruiseState.available = True
  return module, helper, cs


@pytest.mark.parametrize('metric', [False, True])
@pytest.mark.parametrize('experimental', [False, True])
def test_fresh_set_adds_exactly_two_mph_and_then_stays_fixed(cruise, metric, experimental):
  _, helper, cs = cruise
  cs.buttonEvents = [{'type': 'decelCruise', 'pressed': False}]
  helper.update_v_cruise(cs, False, metric)
  helper.initialize_v_cruise(cs, experimental, False)
  expected = round(cs.vEgo * CV.MS_TO_KPH) + 2*CV.MPH_TO_KPH
  assert helper.v_cruise_kph == pytest.approx(expected)
  cs.buttonEvents = []
  for _ in range(100):
    helper.update_v_cruise(cs, True, metric)
    assert helper.v_cruise_kph == pytest.approx(expected)
    assert helper.v_cruise_cluster_kph == helper.v_cruise_kph


@pytest.mark.parametrize('resume', ['accelCruise', 'resumeCruise'])
@pytest.mark.parametrize('delay', [0, 1, 5, 30])
def test_resume_after_brake_restores_saved_speed_despite_ipc_delay(cruise, resume, delay):
  _, helper, cs = cruise
  helper.initialize_v_cruise(cs, False, False)
  saved = helper.v_cruise_kph
  for _ in range(3):  # repeated resumes never compound the offset
    cs.brakePressed = True
    cs.vEgo = 12.
    cs.buttonEvents = []
    helper.update_v_cruise(cs, False, False)
    cs.brakePressed = False
    cs.buttonEvents = [{'type': resume, 'pressed': False}]
    helper.update_v_cruise(cs, False, False)
    cs.buttonEvents = []
    for _ in range(delay):
      helper.update_v_cruise(cs, False, False)
    helper.update_v_cruise(cs, True, False)
    helper.initialize_v_cruise(cs, False, False)
    assert helper.v_cruise_kph == saved
    assert helper._enable_button is None


def test_new_set_after_brake_captures_current_speed(cruise):
  _, helper, cs = cruise
  helper.initialize_v_cruise(cs, False, False)
  helper.update_v_cruise(cs, False, False)
  cs.vEgo = 15.
  cs.buttonEvents = [{'type': 'decelCruise', 'pressed': False}]
  helper.update_v_cruise(cs, False, False)
  cs.buttonEvents = []
  helper.update_v_cruise(cs, True, False)
  helper.initialize_v_cruise(cs, False, False)
  assert helper.v_cruise_kph == pytest.approx(54 + 2*CV.MPH_TO_KPH)


@pytest.mark.parametrize('clear', ['cancel', 'mainCruise', 'unavailable', 'expired'])
def test_cancel_unavailable_or_expired_intent_cannot_affect_future_set(cruise, clear):
  module, helper, cs = cruise
  helper.initialize_v_cruise(cs, False, False)
  cs.buttonEvents = [{'type': 'accelCruise', 'pressed': False}]
  helper.update_v_cruise(cs, False, False)
  cs.buttonEvents = []
  if clear in ('cancel', 'mainCruise'):
    cs.buttonEvents = [{'type': clear, 'pressed': True}]
  elif clear == 'unavailable':
    cs.cruiseState.available = False
  for _ in range(module.ENABLE_BUTTON_MAX_FRAMES+1 if clear == 'expired' else 1):
    helper.update_v_cruise(cs, False, False)
  assert helper._enable_button is None


def test_offset_respects_maximum_and_pcm_ownership(cruise):
  module, helper, cs = cruise
  cs.vEgo = 100.
  helper.initialize_v_cruise(cs, False, False)
  assert helper.v_cruise_kph == module.V_CRUISE_MAX
  helper.CP.pcmCruise = True
  helper.v_cruise_kph = 90.
  helper.initialize_v_cruise(cs, False, False)
  assert helper.v_cruise_kph == 90.


def test_physical_k5_resume_already_produces_enable_on_release():
  # Execute the production parser mapping and enable method without importing
  # native CANParser. This verifies the physical RES mapping, not a copied map.
  import ast
  from opendbc.car import structs
  from opendbc.car.hyundai.values import Buttons
  namespace = {'Buttons': Buttons, 'ButtonType': structs.CarState.ButtonEvent.Type, 'structs': structs}
  tree = ast.parse((ROOT/'opendbc_repo/opendbc/car/hyundai/carstate.py').read_text())
  mapping = next(n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, 'id', '') == 'BUTTONS_DICT' for t in n.targets))
  tree = ast.parse((ROOT/'opendbc_repo/opendbc/car/interfaces.py').read_text())
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'update_button_enable')
  exec(compile(ast.Module(body=[mapping, method], type_ignores=[]), '<production resume path>', 'exec'), namespace)
  physical_type = namespace['BUTTONS_DICT'][Buttons.RES_ACCEL]
  cs = NS(CP=NS(pcmCruise=False))
  for pressed in (True, False):
    button = NS(type=physical_type, pressed=pressed)
    assert namespace['update_button_enable'](cs, [button]) is (not pressed)
  cs.CP.pcmCruise = True
  assert not namespace['update_button_enable'](cs, [button])
