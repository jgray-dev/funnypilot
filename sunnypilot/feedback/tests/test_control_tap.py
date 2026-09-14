import json

from sunnypilot.feedback.control_tap import ControlTap, read_control, MAX_BYTES


def test_diagnostics_roundtrip_and_expiry(tmp_path):
  path = str(tmp_path / 'control')
  tap = ControlTap(path)
  row = {'t': 10.0, 'frame': 100, 'torque_controller': {'friction': .1}, 'applied_torque': .2}
  tap.publish(row)
  assert read_control(10.05, path) == row
  assert read_control(10.11, path) is None
  assert read_control(9.9, path) is None


def test_diagnostics_failures_are_inert_and_corruption_is_not_fresh(tmp_path):
  path = tmp_path / 'control'
  tap = ControlTap(str(path))
  tap.publish({'t': float('nan')})
  assert not path.exists()
  tap.publish({'t': 10., 'oversized': 'x' * MAX_BYTES})
  assert not path.exists()
  ControlTap(str(tmp_path / 'missing' / 'control')).publish({'t': 10.})
  for data in ('[]', 'bad', json.dumps({'t': '10'}), 'x' * (MAX_BYTES + 1)):
    path.write_text(data)
    assert read_control(10., str(path)) is None
