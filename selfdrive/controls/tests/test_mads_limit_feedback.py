"""Exercise the shipped publish block without importing device-only controlsd."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS


def limit_update(previous, lat_active, selfdrive_active, requested, applied):
  tree = ast.parse((Path(__file__).parents[1] / 'controlsd.py').read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controls')
  publish = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'publish')
  block = next(n for n in publish.body if isinstance(n, ast.If) and any(
    isinstance(x, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == 'steer_limited_by_safety' for t in x.targets)
    for x in ast.walk(n)))
  owner = NS(steer_limited_by_safety=previous, CP=NS(steerControlType='torque'),
             sm={'selfdriveState': NS(active=selfdrive_active), 'carOutput': NS(actuatorsOutput=NS(torque=applied))})
  scope = {'self': owner, 'CC': NS(latActive=lat_active, actuators=NS(torque=requested)),
           'car': NS(CarParams=NS(SteerControlType=NS(angle='angle')))}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), 'production-limit-block', 'exec'), scope)
  return owner.steer_limited_by_safety


def test_lateral_only_clears_stale_limit_when_output_tracks_command():
  assert limit_update(True, True, False, .2, .2) is False


def test_lateral_only_detects_real_torque_limit():
  assert limit_update(False, True, False, .4, .2) is True


def test_inactive_lateral_resets_limit_for_next_engagement():
  assert limit_update(True, False, False, .4, .2) is False


def test_full_engagement_still_detects_limits():
  assert limit_update(False, True, True, .4, .2) is True
