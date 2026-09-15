from types import SimpleNamespace as NS

from openpilot.selfdrive.selfdrived.process_health import ProcessHealthMonitor


class Signals(dict):
  def __init__(self):
    super().__init__(radarState=NS(leadOne=NS(status=True, dRel=30.)))
    self.services = ['radarState', 'longitudinalPlan', 'ignored']
    self.valid = dict.fromkeys(self.services, True)
    self.alive = dict.fromkeys(self.services, True)
    self.freq_ok = dict.fromkeys(self.services, True)
    self.recv_time = dict.fromkeys(self.services, 10.)
    self.logMonoTime = dict.fromkeys(self.services, 10_000_000_000)
    self.valid['ignored'] = False

  def all_checks(self, services):
    return all(s == 'ignored' or (self.valid[s] and self.alive[s] and self.freq_ok[s]) for s in services)


def test_records_one_frame_fault_recovery_and_gap_context():
  rows = []
  monitor = ProcessHealthMonitor(NS(write=lambda row: rows.append(row)))
  sm = Signals()
  cs = NS(buttonEvents=[NS(type='gapAdjustCruise', pressed=False)], vEgo=20.)
  monitor.update(sm, cs, 10., False, True, 1)
  assert rows == []
  cs.buttonEvents = []
  sm.valid['longitudinalPlan'] = False
  monitor.update(sm, cs, 10.01, True, True, 0)
  assert rows[0]['last_gap_button'] == {'mono': 10., 'pressed': False}
  assert list(rows[0]['failed']) == ['longitudinalPlan']
  assert rows[0]['failed']['longitudinalPlan']['age_s'] == .01
  sm.valid['longitudinalPlan'] = True
  monitor.update(sm, cs, 10.02, False, True, 0)
  assert rows[1]['recovered'] and rows[1]['duration_s'] == .01
  assert rows[1]['previous_failed'] == ['longitudinalPlan']


def test_stable_fault_is_bounded_but_changed_failure_is_recorded():
  rows = []
  monitor = ProcessHealthMonitor(NS(write=lambda row: rows.append(row)))
  sm, cs = Signals(), NS(buttonEvents=[], vEgo=20.)
  sm.alive['radarState'] = False
  for i in range(500):
    monitor.update(sm, cs, 10. + i * .01, True, True, 1)
  assert len(rows) == 1
  sm.freq_ok['longitudinalPlan'] = False
  monitor.update(sm, cs, 15., True, True, 1)
  assert len(rows) == 2 and rows[-1]['duration_s'] == 5.
  assert list(rows[-1]['failed']) == ['radarState', 'longitudinalPlan']


def test_failed_diagnostic_writer_never_raises():
  def fail(row):
    raise OSError('full')
  monitor = ProcessHealthMonitor(NS(write=fail))
  sm = Signals()
  sm.valid['radarState'] = False
  monitor.update(sm, NS(buttonEvents=[], vEgo=20.), 10., True, True, 1)
