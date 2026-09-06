"""Every msgq service keeps its subscriber count inside the reader table.

msgq_repo/msgq/msgq.h fixes NUM_READERS per service. msgq_init_subscriber()
(msgq_repo/msgq/msgq.cc) evicts every registered reader when one more
subscribes, and with NUM_READERS + 1 live readers they keep evicting each
other, so the slower ones never receive a message. 3.7.1a's feedback recorder
made carState's sixteenth reader; calibrationd and locationd then published
liveCalibration and livePose invalid and the car could not engage
(docs/engagement-3.7.5.md).

This census reads the configured processes and the service literals of their
SubMaster()/sub_sock() calls from source, so a new subscriber on a full service
fails here rather than on the road. It is static: a service list must be
resolvable (literals, concatenation, names assigned from them, `if CONSTANT:`
guards) or named in ASSUMED with its comma 3X value.
"""
import ast
import collections
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROCESS_CONFIG = ROOT / 'system/manager/process_config.py'

# Processes configured in process_config.py that do not run while a comma 3X
# drives a car with default settings. Every other PythonProcess/DaemonProcess
# must be listed in EXTRA_FILES or be a plain module; a new process fails
# test_every_configured_process_is_classified until it is classified here.
NOT_RUNNING = {
  'webcamerad': 'PC webcam build only',
  'joystickd': 'JoystickDebugMode or notCar',
  'joystick': 'JoystickDebugMode',
  'maneuversd': 'LongitudinalManeuverMode',
  'ubloxd': 'ublox GPS hardware (C3X uses qcomgpsd)',
  'pigeond': 'ublox GPS hardware',
  'updated': 'offroad only',
  'uploader': 'offroad unless OnroadUploads',
  'webrtcd': 'notCar',
  'webjoystick': 'notCar',
  'models_manager': 'offroad only',
  'auto_updater': 'offroad only',
  'backup_manager': 'offroad only',
  'sunnylink_registration_manager': 'runs only until sunnylink is registered',
  'sunnylink_uploader': 'EnableSunnylinkUploader off by default',
}
# Optional readers that a default device does not run: sunnylink daemons need a
# registration, and the Astra monitor subscribes only once paired.
OPTIONAL = {'manage_sunnylinkd', 'statsd_sp', 'funnypilot_astra'}

# Where a process builds readers outside its own module, in visiting order
# (a module that assigns a list must precede the one that concatenates it).
EXTRA_FILES = {
  'ui': ['selfdrive/ui/sunnypilot/ui_state.py', 'selfdrive/ui/ui_state.py', 'sunnypilot/sunnylink/sunnylink_state.py'],
  'controlsd': ['sunnypilot/selfdrive/controls/controlsd_ext.py'],
  'funnypilot_astra': ['sunnypilot/astra_link/state.py'],
  'manage_athenad': ['system/athena/athenad.py'],  # the daemon module only spawns athenad
  'manage_sunnylinkd': ['sunnypilot/sunnylink/athena/sunnylinkd.py', 'system/athena/athenad.py'],  # reuses athenad's upload_handler
}
# SubMaster calls inside a function that the process starts on several threads.
THREADS = {('manage_athenad', 'upload_handler'): 4}
# The three model runners subscribe identically and only one is configured;
# the census counts the stock module (test_model_variants_subscribe_identically).
MODEL_VARIANTS = ['selfdrive/modeld/modeld.py', 'sunnypilot/modeld/modeld.py', 'sunnypilot/modeld_v2/modeld.py']
# Processes that are not Python but do subscribe, and the sources naming their services.
NATIVE = {
  'loggerd': ['cereal/services.py'],
  'locationd_llk': ['sunnypilot/selfdrive/locationd/locationd.cc'],
  'pandad': ['selfdrive/pandad/pandad.cc'],  # launched by the Python pandad process
  'manager': ['system/manager/manager.py'],
}
# Names the static reader cannot resolve, with their comma 3X value; None means
# a transient reader (opened and closed per request), which is what the spare
# slot in test_default_configuration_keeps_a_spare_slot is for.
ASSUMED = {
  'gps_location_service': 'gpsLocation', 'self.gps_location_service': 'gpsLocation',
  'gps_location_socket': 'gpsLocation',
  'service': None,  # athenad getMessage RPC
}
NUM_READERS = int(re.search(r'#define NUM_READERS (\d+)', (ROOT / 'msgq_repo/msgq/msgq.h').read_text()).group(1))


def configured_processes():
  procs = {}
  for node in ast.walk(ast.parse(PROCESS_CONFIG.read_text())):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('PythonProcess', 'DaemonProcess'):
      procs[node.args[0].value] = node.args[1].value.replace('.', '/') + '.py'
  return procs


class Readers(ast.NodeVisitor):
  def __init__(self, process):
    self.process, self.consts, self.lists, self.func = process, {}, {}, None
    self.readers, self.unknown = [], set()

  def key(self, node):
    if isinstance(node, ast.Name):
      return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
      return f'{node.value.id}.{node.attr}'
    return None

  def resolve(self, node, unknown=None):
    """Service names of a list-valued expression, or None when it is not one.

    Names that cannot be resolved are added to `unknown` (a subscriber
    argument); a plain assignment of some other list is simply not a service list.
    """
    if isinstance(node, (ast.List, ast.Tuple)):
      out = []
      for e in node.elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
          out.append(e.value)
        elif self.key(e) in ASSUMED:
          out.extend([ASSUMED[self.key(e)]] if ASSUMED[self.key(e)] else [])
        elif unknown is not None:
          unknown.add(self.key(e) or ast.dump(e))
        else:
          return None
      return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
      left, right = self.resolve(node.left, unknown), self.resolve(node.right, unknown)
      return None if left is None or right is None else left + right
    key = self.key(node)
    if key in self.lists:
      return list(self.lists[key])
    if key in ASSUMED:
      return [ASSUMED[key]] if ASSUMED[key] else []
    return None

  def visit_FunctionDef(self, node):
    previous, self.func = self.func, node.name
    self.generic_visit(node)
    self.func = previous

  visit_AsyncFunctionDef = visit_FunctionDef

  def visit_If(self, node):
    if isinstance(node.test, ast.Name) and node.test.id in self.consts:
      for child in (node.body if self.consts[node.test.id] else node.orelse):
        self.visit(child)
    else:
      self.generic_visit(node)

  def visit_Assign(self, node):
    if self.func is None and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant):
      self.consts[node.targets[0].id] = node.value.value
    services = self.resolve(node.value)
    if services is not None:
      for target in node.targets:
        if self.key(target):
          self.lists[self.key(target)] = services
    self.generic_visit(node)

  def visit_AugAssign(self, node):
    services = self.resolve(node.value)
    if isinstance(node.op, ast.Add) and services is not None and self.key(node.target) in self.lists:
      self.lists[self.key(node.target)] = self.lists[self.key(node.target)] + services
    self.generic_visit(node)

  def visit_ListComp(self, node):
    # [sub_sock(which) for which in ['accelerometer', 'gyroscope']] (locationd)
    elt, gen = node.elt, node.generators[0]
    if (isinstance(elt, ast.Call) and self.callee(elt) == 'sub_sock' and elt.args and
        self.key(elt.args[0]) == self.key(gen.target) and self.resolve(gen.iter) is not None):
      self.readers.extend(self.resolve(gen.iter))
      return
    self.generic_visit(node)

  def callee(self, node):
    return node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None

  def visit_Call(self, node):
    callee = self.callee(node)
    if callee == 'SubMaster' and node.args:
      services = self.resolve(node.args[0], self.unknown)
      if services is None:
        self.unknown.add(self.key(node.args[0]) or ast.dump(node.args[0]))
      else:
        self.readers.extend(services * THREADS.get((self.process, self.func), 1))
    elif callee == 'sub_sock' and node.args:
      arg = node.args[0]
      if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        self.readers.append(arg.value)
      elif self.key(arg) in ASSUMED:
        self.readers.extend([ASSUMED[self.key(arg)]] if ASSUMED[self.key(arg)] else [])
      else:
        self.unknown.add(self.key(arg) or ast.dump(arg))
    self.generic_visit(node)


def native_readers(path):
  text = (ROOT / path).read_text()
  if path.endswith('services.py'):
    spec = importlib.util.spec_from_file_location('fp_services', ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [name for name, service in module.SERVICE_LIST.items() if service.should_log], set()
  if path.endswith('.py'):
    return None, None
  readers, unknown = [], set()
  for block in re.findall(r'(?:service_list\s*=|SubMaster\s+\w+\s*\()\s*\{([^}]*)\}', text):
    readers.extend(re.findall(r'"(\w+)"', block))
    for name in re.findall(r'(?<!")\b([a-z_]+)\b(?!")', re.sub(r'"[^"]*"', '', block)):
      if name in ASSUMED:
        readers.extend([ASSUMED[name]] if ASSUMED[name] else [])
      else:
        unknown.add(name)
  readers.extend(re.findall(r'SubSocket::create\([^;]*?"(\w+)"', text))
  return readers, unknown


def census(include_optional):
  """{service: [process, ...]} for the configured processes, plus unresolved names."""
  readers, unknown = collections.defaultdict(list), {}
  processes = {name: [module] for name, module in configured_processes().items() if name not in NOT_RUNNING}
  for name, extra in EXTRA_FILES.items():
    processes[name] = extra + processes.get(name, [])
  for name, files in NATIVE.items():
    processes[name] = files
  for name, files in processes.items():
    if name in OPTIONAL and not include_optional:
      continue
    visitor = Readers(name)
    for path in files:
      services, unresolved = native_readers(path)
      if services is None:
        visitor.visit(ast.parse((ROOT / path).read_text()))
      else:
        visitor.readers.extend(services)
        visitor.unknown |= unresolved
    for service in visitor.readers:
      readers[service].append(name)
    if visitor.unknown:
      unknown[name] = visitor.unknown
  return readers, unknown


def over(readers, limit):
  return {service: sorted(procs) for service, procs in readers.items() if len(procs) > limit}


def test_every_configured_process_is_classified():
  configured = set(configured_processes())
  stale = (set(NOT_RUNNING) | set(EXTRA_FILES) | OPTIONAL | {p for p, _ in THREADS}) - configured
  assert not stale, f'entries no longer in process_config: {stale}'
  assert {'funnypilot_feedback', 'feedbackd', 'calibrationd', 'locationd', 'controlsd', 'ui', 'manage_athenad'} <= configured - set(NOT_RUNNING)
  for path in [*MODEL_VARIANTS, *(f for files in [*EXTRA_FILES.values(), *NATIVE.values()] for f in files)]:
    assert (ROOT / path).is_file(), path


def test_model_variants_subscribe_identically():
  lists = []
  for path in MODEL_VARIANTS:
    visitor = Readers(path)
    visitor.visit(ast.parse((ROOT / path).read_text()))
    lists.append(sorted(visitor.readers))
  assert lists[0] and lists.count(lists[0]) == len(lists), lists


def test_every_service_list_is_resolved():
  _, unknown = census(include_optional=True)
  assert not unknown, f'add these to ASSUMED with their comma 3X value: {unknown}'


def test_census_sees_the_known_carstate_readers():
  readers, _ = census(include_optional=False)
  expected = {'loggerd', 'selfdrived', 'controlsd', 'plannerd', 'radard', 'lagd', 'torqued', 'calibrationd',
              'locationd', 'paramsd', 'modeld', 'dmonitoringd', 'ui', 'locationd_llk'}
  assert expected <= set(readers['carState'])
  assert 'funnypilot_feedback' not in readers['carState'] and 'feedbackd' not in readers['carState']
  assert 'funnypilot_feedback' not in readers['deviceState'] and 'funnypilot_astra' not in readers['deviceState']


def test_default_configuration_keeps_a_spare_slot():
  # One slot stays free for transient readers (athenad getMessage, debug tools):
  # a transient sixteenth reader costs two evictions before the table settles.
  readers, _ = census(include_optional=False)
  assert not over(readers, NUM_READERS - 1), over(readers, NUM_READERS - 1)


def test_optional_services_never_exceed_the_table():
  readers, _ = census(include_optional=True)
  assert not over(readers, NUM_READERS), over(readers, NUM_READERS)


@pytest.mark.parametrize('module', ['sunnypilot/feedback/feedbackd.py', 'selfdrive/ui/feedback/feedbackd.py'])
def test_feedback_processes_hold_no_carstate_reader(module):
  visitor = Readers(module)
  visitor.visit(ast.parse((ROOT / module).read_text()))
  assert 'carState' not in visitor.readers and 'deviceState' not in visitor.readers, visitor.readers
