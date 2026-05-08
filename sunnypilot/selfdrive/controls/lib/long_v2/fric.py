"""
FunnyPilot LongV2 — friction coefficient helpers.
"""

_FRIC_DEFAULT = 0.8
_FRIC_MIN = 0.15
_FRIC_MAX = 1.0
_FRIC_WET_THRESHOLD = 0.6


def get_fric(sm) -> float:
  try:
    fric = sm["liveParameters"].frictionCoefficientFiltered
    if fric <= 0 or fric != fric:  # zero, negative, or NaN
      return _FRIC_DEFAULT
    return max(_FRIC_MIN, min(_FRIC_MAX, fric))
  except Exception:
    return _FRIC_DEFAULT


def comfort_scale(fric: float) -> float:
  return max(0.4, min(1.0, fric / _FRIC_DEFAULT))


def weather_cap_active(fric: float) -> bool:
  return fric < _FRIC_WET_THRESHOLD


def weather_speed_scale(fric: float) -> float:
  return max(0.5, min(1.0, fric / _FRIC_DEFAULT))
