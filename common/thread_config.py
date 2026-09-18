"""Scheduling for background workers, without importing device/native modules."""
import os


HOUSEKEEPING_CPUS = (0, 1, 2, 3)


def configure_background_thread():
  """Drop inherited FIFO priority AND control-core affinity in the calling thread.

  C3X keeps cores 0–3 online even offroad. On hosts without those CPUs or these
  Linux APIs, leave the permitted affinity alone. Attempt both independently.
  """
  try:
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
  except (AttributeError, OSError):
    pass
  try:
    os.sched_setaffinity(0, HOUSEKEEPING_CPUS)
  except (AttributeError, OSError):
    pass
