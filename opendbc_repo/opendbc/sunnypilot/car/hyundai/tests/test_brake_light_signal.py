"""FunnyPilot v3.4.4 — TCS13.BrakeLight must exist in the classic-CAN DBC.

WHY THIS TEST EXISTS. `CarStateExt.update` does `cp.vl["TCS13"]["BrakeLight"]`
at the 100 Hz CarState rate, inside card. A CANParser signal dict raises
KeyError on an unknown name, so a typo or an upstream DBC rename would not
degrade the status dot — it would kill card on the first frame, i.e. a car that
does not drive. That is the same failure class as the v3.4.0/v3.4.1 boot brick,
and the standing rule from those post-mortems applies: anything on a process's
startup/hot path gets a guard that fails HERE, off-device, not in the driveway.

This reads the .dbc as text on purpose — no CANParser, no compiled opendbc
extension — so it runs in any environment.
"""
import pathlib
import re

import pytest

DBC_DIR = pathlib.Path(__file__).resolve().parents[4] / 'dbc'
CLASSIC_DBC = DBC_DIR / 'hyundai_kia_generic.dbc'


def _signals(dbc_text: str, message: str) -> set[str]:
  """Signal names defined under `BO_ <id> <message>:` up to the next BO_."""
  m = re.search(rf'^BO_ \d+ {re.escape(message)}:.*?$', dbc_text, re.M)
  if m is None:
    return set()
  rest = dbc_text[m.end():]
  end = re.search(r'^BO_ ', rest, re.M)
  body = rest[:end.start()] if end else rest
  return set(re.findall(r'^\s*SG_ (\w+)\s*:', body, re.M))


@pytest.fixture(scope='module')
def tcs13() -> set[str]:
  assert CLASSIC_DBC.is_file(), f'missing {CLASSIC_DBC}'
  return _signals(CLASSIC_DBC.read_text(), 'TCS13')


def test_parser_finds_the_message_at_all(tcs13):
  # self-check: if the regex ever stops matching, the assertions below would
  # pass vacuously on an empty set
  assert len(tcs13) > 5


def test_brake_light_signal_is_defined(tcs13):
  assert 'BrakeLight' in tcs13


def test_signals_already_relied_on_are_still_there(tcs13):
  # aBasis is read on the same line in carstate_ext; DriverOverride/PBRAKE_ACT
  # in upstream carstate. If TCS13 is ever restructured these go together.
  for sig in ('aBasis', 'DriverOverride', 'PBRAKE_ACT'):
    assert sig in tcs13


def test_carstate_ext_reads_the_signal_by_that_exact_name():
  src = (pathlib.Path(__file__).resolve().parents[1] / 'carstate_ext.py').read_text()
  assert 'cp.vl["TCS13"]["BrakeLight"]' in src
