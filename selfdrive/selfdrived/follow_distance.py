"""Serialize follow-distance persistence without blocking the control loop."""
import threading


class FollowDistanceSetting:
  def __init__(self, value):
    self._value = value
    self._revision = 0
    self._pending = False
    self._lock = threading.Lock()

  def snapshot(self):
    with self._lock:
      return self._value

  def cycle(self):
    with self._lock:
      self._value = (self._value - 1) % 3
      self._revision += 1
      self._pending = True
      return self._value

  def sync(self, params):
    """Called only by the Params worker; disk IO never holds the state lock.

    A read started before a button press cannot replace that press. Pending
    writes are serialized here, and only acknowledged by a matching readback.
    Multiple presses during a slow write coalesce to the latest setting.
    """
    with self._lock:
      revision, value, pending = self._revision, self._value, self._pending
    try:
      if pending:
        params.put('LongitudinalPersonality', value)
      observed = params.get('LongitudinalPersonality', return_default=True)
      if type(observed) is not int or observed not in (0, 1, 2):
        return False
    except Exception:
      return False
    with self._lock:
      if revision != self._revision:
        return True  # a newer button request will be persisted next time
      if pending and observed != value:
        return False  # failed/unacknowledged write; retain the requested value
      self._value, self._pending = observed, False
    return True
