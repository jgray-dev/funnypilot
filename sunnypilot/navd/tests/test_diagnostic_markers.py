"""Run the shipped diagnostic shell template against the actual source files."""
import ast
from pathlib import Path
import subprocess
import shlex

ROOT = Path(__file__).resolve().parents[3]


def test_all_diagnostic_markers_resolve_with_the_shipped_command():
  tree = ast.parse((ROOT / 'sunnypilot/navd/nav_webserver.py').read_text())
  assignments = {target.id:node.value for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets if isinstance(target, ast.Name)}
  markers = ast.literal_eval(assignments['_CODE_MARKERS'])
  assert len(markers) > 100
  local = [(pattern, str(ROOT / path.removeprefix('/data/openpilot/')), label) for pattern, path, label in markers]
  command = eval(compile(ast.Expression(assignments['_CODE_CMD']), '<diagnostic command>', 'eval'), {'_CODE_MARKERS':local, 'shlex':shlex})
  output = subprocess.check_output(['sh', '-c', command], text=True)
  assert 'MISSING' not in output, output
  assert len(output.splitlines()) == len(markers)
