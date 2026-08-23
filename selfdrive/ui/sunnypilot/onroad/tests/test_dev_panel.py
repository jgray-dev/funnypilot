"""FunnyPilot v3.6.7 — the developer panel as an instrument.

WHY THIS FILE EXISTS AND WHY IT DRIVES THE REAL ELEMENTS. The panel is the only
way v3.6.7's three behavioural changes can be checked from the seat: the
actuator's tracking of the plan, the stop-and-go governor, and which of SCC-V
and SCC-M is deciding. A readout that silently shows a stale zero, or that
drops a slot and slides every other value sideways, is worse than no readout —
so those two properties are pinned here rather than assumed.

`elements.py` imports pyray, which does not exist off-device. It is loaded with
the same stubbing `test_long_status_dot.py` uses, and then the ACTUAL element
objects are driven with fake `sm` messages. An AST scan would not have caught
the things this catches.
"""
import ast
import importlib.util
import math
import pathlib

import pytest

_ONROAD = pathlib.Path(__file__).resolve().parents[1]
_ELEMENTS = _ONROAD / 'developer_ui' / 'elements.py'
_DEVUI = _ONROAD / 'developer_ui' / '__init__.py'
_CHROME = _ONROAD / 'hud' / 'chrome.py'


def _stub():
  """REUSE test_hud_imports' stub rather than installing a second one.

  `sys.modules` is process-wide and pytest collects these files in one process,
  so whichever test file gets there first defines pyray for ALL of them. The
  first cut of this file installed its own thinner stub, won the race by
  alphabetical order, and broke test_hud_logic's collection with
  `module 'pyray' has no attribute 'Font'` — a failure in a file this release
  does not touch. One stub, one owner.
  """
  from openpilot.selfdrive.ui.sunnypilot.onroad.tests.test_hud_imports import _stub_graphics
  _stub_graphics()


def _load(path, name):
  _stub()
  spec = importlib.util.spec_from_file_location(name, path)
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


E = _load(_ELEMENTS, '_fp_elements')


class _Obj:
  def __init__(self, **kw):
    self.__dict__.update(kw)


def _sm(long_active=True, cmd=0.0, out=0.0, lead=None):
  """The four cereal messages the longitudinal row reads, and nothing else."""
  return {
    'carControl': _Obj(longActive=long_active, actuators=_Obj(accel=cmd)),
    'carOutput': _Obj(actuatorsOutput=_Obj(accel=out)),
    'radarState': _Obj(leadOne=lead if lead is not None else _Obj(status=False, dRel=0.0, vRel=0.0)),
  }


def _debug(**over):
  """A full 20-field payload with everything quiet, overridden by index."""
  row = list(E._SCC_CACHE)
  for k, v in over.items():
    row[int(k[1:])] = v
  return tuple(row)


@pytest.fixture(autouse=True)
def _quiet_shm(monkeypatch):
  """Pin the /dev/shm read so a device file (or its absence) cannot leak in."""
  state = {'row': tuple(E._SCC_CACHE)}
  monkeypatch.setattr(E, '_scc_debug', lambda: state['row'])
  return state


# ── the no-reflow rule ──────────────────────────────────────────────────────

_ROW_ELEMENTS = [
  'LongSourceElement', 'LongAccelElement', 'LongTrackingElement',
  'LeadElement', 'StopGovernorElement', 'SccGateElement',
  'SccCornerSpeedElement', 'SccCapElement', 'SccVisionCapElement',
  'SccCorroborationElement', 'SccAuthorityElement',
]


class TestNothingEverDropsItsSlot:
  """THE v3.5.9 STATION RULE, applied to the panel. Both rows divide a fixed
  width between however many elements they are handed, so an element returning
  None — or raising — would not merely go blank, it would re-centre every other
  value in its row. A readout that moves is a readout you stop trusting."""

  @pytest.mark.parametrize("name", _ROW_ELEMENTS)
  def test_a_dead_car_still_yields_a_labelled_element(self, name):
    el = getattr(E, name)()
    got = el.update(_sm(long_active=False), False)
    assert isinstance(got, E.UiElement)
    assert got.label and got.value

  @pytest.mark.parametrize("name", _ROW_ELEMENTS)
  def test_missing_messages_do_not_raise(self, name):
    el = getattr(E, name)()
    assert isinstance(el.update({}, False), E.UiElement)


# ── the v3.6.7 headline readout ─────────────────────────────────────────────

class _Clock:
  """A FAKE FRAME CLOCK, and it is not a convenience.

  `LongTrackingElement` reads `time.monotonic()` itself, exactly once per frame,
  the way `tokens.Eased` does — so its rate is a function of REAL elapsed time
  rather than of a per-call fraction. That is the v3.5.4 property that keeps
  the feel identical when the device throttles, and it is why a test that just
  loops sees dt ~ 0 and measures nothing at all. Frames are stepped explicitly
  at 60 Hz here.
  """
  def __init__(self, dt=1.0 / 60.0):
    self.t = 1000.0
    self.dt = dt

  def monotonic(self):
    return self.t

  def step(self):
    self.t += self.dt


class TestTheTrackingReadout:
  """TRK — how far the actuator's output is behind the planner's command.

  Both operands are published signals (`carControl.actuators.accel` is what the
  controller assigns to accel_cmd; `carOutput.actuatorsOutput.accel` is what
  the jerk-limited integrator produced), so this is a MEASUREMENT rather than a
  second copy of the controller's maths — the distinction the v3.4.9 physics
  post-mortem is about.
  """

  @pytest.fixture(autouse=True)
  def _clock(self, monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(E.time, 'monotonic', clk.monotonic)
    self.clk = clk
    return clk

  def _frame(self, el, cmd, out, **kw):
    self.clk.step()
    return el.update(_sm(cmd=cmd, out=out, **kw), False)

  def _settle(self, el, cmd, out, n=200):
    for _ in range(n):
      got = self._frame(el, cmd, out)
    return got

  def test_perfect_tracking_reads_zero(self):
    el = E.LongTrackingElement()
    got = self._settle(el, -2.0, -2.0)
    assert float(got.value) == pytest.approx(0.0, abs=0.01)

  def test_the_measured_pure_p_lag_reads_as_the_lag(self):
    # 0.54 m/s^2 is what the old law left on a 2 m/s^3 brake ramp, measured
    # through the real controller. The panel has to show that, not round it
    # away, or the number cannot be used to tell the two builds apart.
    el = E.LongTrackingElement()
    got = self._settle(el, -2.00, -1.46)
    assert float(got.value) == pytest.approx(0.54, abs=0.02)

  def test_it_is_a_magnitude_so_a_lagging_throttle_shows_too(self):
    el = E.LongTrackingElement()
    got = self._settle(el, 1.00, 0.60)
    assert float(got.value) == pytest.approx(0.40, abs=0.02)

  def test_it_is_low_passed_rather_than_instantaneous(self):
    # A step in the command produces a legitimate transient — that IS the
    # lag doing its job — so one frame of divergence must not paint the panel
    # red. Sustained divergence must.
    el = E.LongTrackingElement()
    self._frame(el, 0.0, 0.0)
    one = self._frame(el, -2.0, 0.0)
    assert float(one.value) < 0.5
    many = self._settle(el, -2.0, 0.0)
    assert float(many.value) > 1.5

  def test_inactive_long_reads_a_dash_and_forgets(self):
    el = E.LongTrackingElement()
    self._settle(el, -2.0, 0.0)
    self.clk.step()
    assert el.update(_sm(long_active=False), False).value == "-"
    # ...and re-engaging does not resume from the old error.
    assert float(self._frame(el, 0.0, 0.0).value) == pytest.approx(0.0, abs=0.01)

  def test_a_stalled_frame_cannot_teleport_it(self):
    # A backgrounded or dropped-frame gap must not hand the filter its whole
    # step in one go — the same _EASE_DT_MAX property tokens.py carries.
    el = E.LongTrackingElement()
    self._settle(el, 0.0, 0.0)
    self.clk.t += 10.0                 # ten seconds of missing frames
    el.update(_sm(cmd=-2.0, out=0.0), False)
    # Asserted on the FILTER STATE, not on the two-decimal display: the bound
    # is exact and rounding the value first would make the test pass for a
    # clamp that is up to 0.005 wrong.
    ceiling = 2.0 * (1.0 - math.exp(-E._TRK_DT_MAX / E._TRK_TAU))
    assert el._v <= ceiling + 1e-9
    # ...and without the clamp ten seconds would have taken it essentially the
    # whole way, so the guard is doing real work rather than being vacuous.
    assert el._v < 1.2


class TestTheSourceReadout:
  """SRC — classified by the planner, looked up here."""

  def test_it_names_the_published_code(self, _quiet_shm):
    for code, name in E._SRC_NAMES.items():
      _quiet_shm['row'] = _debug(f19=code)
      assert E.LongSourceElement().update(_sm(), False).value == name

  def test_an_unknown_code_is_visibly_unknown(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f19=99)
    assert E.LongSourceElement().update(_sm(), False).value == "?"

  def test_the_names_come_from_the_controller(self):
    from openpilot.selfdrive.controls.lib.long_shaping import SRC_NAMES
    assert E._SRC_NAMES == SRC_NAMES


class TestTheStopGovernorReadout:
  def test_off_when_it_is_not_constraining(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f18=0.0)
    assert E.StopGovernorElement().update(_sm(), False).value == "off"

  def test_it_shows_the_cap_in_display_units(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f18=13.4)          # 30 mph
    got = E.StopGovernorElement().update(_sm(), False)
    assert got.value == "30" and got.unit == "mph"
    got = E.StopGovernorElement().update(_sm(), True)
    assert got.value == "48" and got.unit == "km/h"


class TestTheVisionReadouts:
  """SCC-V's cap and the model's own corroboration, which is what v3.6.7 made
  supreme over the map."""

  def test_the_corroboration_threshold_is_the_fusions_own(self):
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_fusion import VISION_DISAGREE_TH
    assert E._VETO_TH == VISION_DISAGREE_TH

  def test_below_the_threshold_reads_as_would_veto(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f17=0.10)
    got = E.SccCorroborationElement().update(_sm(), False)
    assert got.value == "0.10" and got.color == E._GREEN

  def test_above_it_reads_as_vision_agrees(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f17=1.00)
    got = E.SccCorroborationElement().update(_sm(), False)
    assert got.value == "1.00" and got.color == E._AMBER

  def test_scc_v_shows_a_dash_when_it_has_nothing_to_say(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f16=0.0)
    assert E.SccVisionCapElement().update(_sm(), False).value == "-"

  def test_scc_v_and_scc_m_are_both_visible_at_once(self, _quiet_shm):
    # The whole point of the fusion change is that the two can disagree, so
    # the panel must be able to show two different numbers simultaneously.
    _quiet_shm['row'] = _debug(f16=13.0, f7=18.0, f2=13.0)
    assert E.SccVisionCapElement().update(_sm(), False).value == "29"
    assert E.SccCapElement().update(_sm(), False).value == "40"



class TestAuthStillReadsAsARestingDash:
  """v3.6.7's fourth item, kept pinned now that AUTH shares a panel with more
  elements that use the same convention."""

  def test_no_corner_is_a_grey_dash_with_no_unit(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f2=0.0, f8=0.0)
    got = E.SccAuthorityElement().update(_sm(), False)
    assert (got.value, got.unit, got.color) == ("-", "", E._GREY)

  def test_a_vetoed_corner_is_a_red_zero(self, _quiet_shm):
    _quiet_shm['row'] = _debug(f2=13.0, f8=0.0)
    got = E.SccAuthorityElement().update(_sm(), False)
    assert (got.value, got.unit, got.color) == ("0", "%", E._RED)


# ── the guards that exist because this release broke them ───────────────────

class TestThePayloadIsLongEnoughForEverythingThatReadsIt:
  """THE DEFECT THIS FILE FOUND, PINNED SO IT CANNOT COME BACK.

  Every element indexes the payload positionally. `elements.py` used to carry a
  HAND-TYPED 16-tuple as its resting value for when the lazy `scc_shm` import
  fails; v3.6.7 appended four fields, so `_scc_debug()[19]` raised IndexError —
  in the UI process, at 60 Hz, i.e. a widget disabled for the session and (for
  anything outside `safe_draw`) a dead UI.

  A hand-written copy of a wire format cannot notice that the format grew. It
  is the same shape as v3.6.3's five-name unpack of a six-field corner, one
  layer further out, and the answer is the same: take the producer's own value.
  """

  def _max_index(self):
    tree = ast.parse(_ELEMENTS.read_text())
    idx = []
    for node in ast.walk(tree):
      if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call)
          and isinstance(node.value.func, ast.Name) and node.value.func.id == '_scc_debug'
          and isinstance(node.slice, ast.Constant)):
        idx.append(int(node.slice.value))
    return idx

  def test_the_scan_actually_finds_the_reads(self):
    # Anti-vacuous: a scan that matches nothing passes the real check
    # trivially. Bounded below by the number of SHM-backed elements rather
    # than by a number typed once — every element on the SCC side reads at
    # least one field, so the scan must see at least that many.
    shm_elements = ('SccCornerSpeedElement', 'SccGateElement', 'SccCapElement',
                    'SccAuthorityElement', 'SccVisionCapElement',
                    'SccCorroborationElement', 'LongSourceElement',
                    'StopGovernorElement')
    src = _ELEMENTS.read_text()
    assert all(f'class {n}' in src for n in shm_elements)
    assert len(self._max_index()) >= len(shm_elements)

  def test_the_resting_payload_covers_every_index_read(self):
    assert max(self._max_index()) < len(E._SCC_INACTIVE)

  def test_the_resting_payload_is_the_publishers_own(self):
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import DEBUG_INACTIVE
    assert E._SCC_INACTIVE == DEBUG_INACTIVE


class TestTheWireFormatContract:
  """Driven by the REAL producer, never by a hand-built fixture.

  v3.6.3 lost the minimap for a whole release because `TestCornerSpeedAt` built
  its corner tuples by hand and so could not notice that the producer had
  changed shape. The same trap applies here with four more fields, so this
  round-trips SCCMapV2.debug_row -> write -> read and checks the ends line up.
  """

  def test_debug_row_matches_what_the_reader_returns(self, tmp_path, monkeypatch):
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_map_v2 import SCCMapV2
    monkeypatch.setattr(scc_shm, 'DEBUG_SHM_PATH', str(tmp_path / 'dbg'))

    scc = SCCMapV2()
    row = scc.debug_row(0.75, 13.0, 0.42, 11.0, 3)
    assert len(row) == len(scc_shm.DEBUG_INACTIVE)

    scc_shm.write_scc_debug_shm(row)
    back = scc_shm.read_scc_debug_shm()
    assert len(back) == len(row)
    # The four v3.6.7 fields specifically, by index, so an insertion in the
    # middle of the format fails here rather than silently relabelling a
    # neighbour.
    assert back[16] == pytest.approx(13.0)
    assert back[17] == pytest.approx(0.42)
    assert back[18] == pytest.approx(11.0)
    assert back[19] == 3

  def test_a_short_line_reads_as_inactive_rather_than_defaulted(self, tmp_path, monkeypatch):
    # Version skew must lose the whole panel, not fill the tail with zeros that
    # look like live readings.
    from openpilot.sunnypilot.selfdrive.controls.lib.long_v2 import scc_shm
    p = tmp_path / 'dbg'
    monkeypatch.setattr(scc_shm, 'DEBUG_SHM_PATH', str(p))
    import time as _t
    p.write_text(",".join(["0"] * 16) + f",{_t.monotonic():.3f}")
    assert scc_shm.read_scc_debug_shm() == scc_shm.DEBUG_INACTIVE


class TestTheSingleRowGeometry:
  """ARITHMETIC, NOT EYE — none of this can be rendered off-device, and the
  v3.5.0 draft that pushed a column under the minimap put five 120 px elements
  on a 104 px pitch and ran the last one off the screen.

  ONE ROW AND ONE COLUMN IS A DELIBERATE CEILING. A row divides a fixed width
  between however many elements it is handed, so every readout added shrinks
  every readout already there; a second row was tried in v3.6.7 and removed
  because the alternative cost is windscreen. These tests exist so the next
  addition has to displace something rather than quietly grow the panel.
  """

  def _devui_src(self):
    return _DEVUI.read_text()

  def test_the_bar_is_one_row(self):
    src = self._devui_src()
    ns = {}
    for line in src.splitlines():
      t = line.strip()
      for k in ('ROW_H', 'BOTTOM_ROWS', 'BOTTOM_BAR_HEIGHT'):
        if t.startswith(k + ' ='):
          ns[k] = t.split('=', 1)[1].strip()
    assert ns['ROW_H'] == '61'
    assert ns['BOTTOM_ROWS'] == '1'
    assert ns['BOTTOM_BAR_HEIGHT'] == 'ROW_H * BOTTOM_ROWS'

  def test_the_bar_fits_inside_the_bottom_horizon_band(self):
    # The band is what makes text legible over road. A panel taller than it
    # would put readouts on bare camera image at the top edge.
    band = None
    for line in _CHROME.read_text().splitlines():
      if line.startswith('BAND_BOT_H'):
        band = int(line.split('=')[1].split('#')[0].strip())
    assert band is not None
    assert 61 <= band

  def _elements_of(self, fn_name, var):
    import ast
    tree = ast.parse(self._devui_src())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    assign = next((n for n in ast.walk(fn)
                   if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == var), None)
    assert assign is not None, f"{var} moved; re-point this guard"
    assert isinstance(assign.value, ast.List)
    return assign.value.elts

  def test_the_bar_is_one_flat_unconditional_list(self):
    # Structural, not textual — v3.6.6 wrote a guard a docstring could break by
    # matching on text. A nested list here would be a second row sneaking back.
    els = self._elements_of('_draw_bottom_dev_ui', 'elements')
    assert 4 <= len(els) <= 7, "the bar is a budget; displace something first"
    for call in els:
      assert isinstance(call, ast.Call), "an element must be an unconditional update() call"

  def test_the_column_is_five_and_unconditional(self):
    els = self._elements_of('_draw_right_dev_ui', 'elements')
    assert len(els) == 5, "five is what RIGHT_TOP_OFFSET + 5 * RIGHT_PITCH fits"
    for call in els:
      assert isinstance(call, ast.Call)

  def test_the_column_pitch_still_fits_the_content_area(self):
    # RIGHT_TOP_OFFSET + n * RIGHT_PITCH must land inside a 1020 px content
    # area, which is what the v3.5.0 draft got wrong by eye.
    src = self._devui_src()
    vals = {}
    for line in src.splitlines():
      for k in ('RIGHT_TOP_OFFSET', 'RIGHT_PITCH'):
        if line.startswith(k + ' ='):
          vals[k] = int(line.split('=')[1].split('#')[0].strip())
    n = len(self._elements_of('_draw_right_dev_ui', 'elements'))
    assert 30 + vals['RIGHT_TOP_OFFSET'] + n * vals['RIGHT_PITCH'] <= 1020

  def test_the_bar_is_the_longitudinal_loop(self):
    labels = [ast.unparse(c.func) for c in self._elements_of('_draw_bottom_dev_ui', 'elements')]
    for want in ('self.long_source.update', 'self.long_accel.update',
                 'self.long_tracking.update', 'self.stop_gov.update',
                 'self.lead.update'):
      assert want in labels

  def test_the_column_is_the_corner_argument(self):
    labels = [ast.unparse(c.func) for c in self._elements_of('_draw_right_dev_ui', 'elements')]
    # SCC-M's ask, SCC-V's ask, the model's own reading, and the verdict.
    for want in ('self.scc_cap.update', 'self.scc_vision_cap.update',
                 'self.scc_corroboration.update', 'self.scc_authority.update'):
      assert want in labels

  def test_the_learning_readouts_are_gone(self):
    # The owner's instruction, pinned: this panel is longitudinal behaviour.
    # Re-adding one is fine — it has to displace something, not append.
    src = _ELEMENTS.read_text()
    for gone in ('SccCornersElement', 'SccRadiusElement', 'SccDistanceElement',
                 'SccALatElement', 'SccVisitsElement', 'SccLearnedElement',
                 'SccLastPassElement', 'SccLaneDepartElement'):
      assert f'class {gone}' not in src
