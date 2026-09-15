"""Bounded communication-fault evidence; no new subscribers or control decisions."""
import math


class ProcessHealthMonitor:
  def __init__(self, recorder):
    self.recorder = recorder
    self.signature = ()
    self.started = None
    self.last_gap = None

  def update(self, sm, cs, now, fault, enabled, personality):
    try:
      for button in cs.buttonEvents:
        if str(button.type) == 'gapAdjustCruise':
          self.last_gap = {'mono': now, 'pressed': bool(button.pressed)}
      failed = [s for s in sm.services if not sm.all_checks([s])] if fault else []
      signature = tuple((s, bool(sm.valid[s]), bool(sm.alive[s]), bool(sm.freq_ok[s])) for s in failed)
      if signature == self.signature:
        return
      previous = self.signature
      self.signature = signature
      if signature and self.started is None:
        self.started = now
      details = {}
      for service in failed:
        age = now - sm.recv_time[service]
        details[service] = {'valid': bool(sm.valid[service]), 'alive': bool(sm.alive[service]),
                            'freq_ok': bool(sm.freq_ok[service]),
                            'age_s': round(age, 4) if math.isfinite(age) else None,
                            'publisher_mono_ns': sm.logMonoTime[service]}
      row = {'kind': 'process_health', 'mono': now, 'failed': details,
             'recovered': not bool(signature), 'previous_failed': [s[0] for s in previous],
             'duration_s': round(now - self.started, 4) if self.started is not None else 0.,
             'enabled': bool(enabled), 'personality': str(personality),
             'v_ego': float(cs.vEgo), 'last_gap_button': self.last_gap,
             'lead': {'status': bool(sm['radarState'].leadOne.status),
                      'd_rel': float(sm['radarState'].leadOne.dRel)}}
      self.recorder.write(row)
      if not signature:
        self.started = None
    except Exception:
      # Diagnostics cannot interrupt the state machine, including during startup.
      pass
