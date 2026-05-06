"""
FunnyPilot LongV2 — friction coefficient helpers.

v2.0.1 calibration based on observed device data:
  - Typical FRIC values sit in the range 0.08 — 1.1 (not 0.15 — 1.0)
  - Values below 0.092 correlate with rain or snow conditions
  - Dry-pavement nominal is around 0.5 (not 0.8)

So the modifiers are recentered: 1.0 = nominal dry pavement, derate
linearly to 0.5 at FRIC = 0.092 (the wet/snow boundary), and weather
cap activates below that boundary.
"""

_FRIC_DEFAULT = 0.5      # nominal dry pavement
_FRIC_MIN = 0.05
_FRIC_MAX = 1.2
_FRIC_WET_THRESHOLD = 0.092


def get_fric(sm) -> float:
  try:
    fric = sm["liveParameters"].frictionCoefficientFiltered
    if fric <= 0 or fric != fric:  # zero, negative, NaN
      return _FRIC_DEFAULT
    return max(_FRIC_MIN, min(_FRIC_MAX, fric))
  except Exception:
    return _FRIC_DEFAULT


def comfort_scale(fric: float) -> float:
  """
  Scale comfort decel/accel limits down on slippery surfaces.
  Returns 1.0 when FRIC ≥ nominal dry, ramps to 0.5 at the wet boundary.
  """
  if fric >= _FRIC_DEFAULT:
    return 1.0
  if fric <= _FRIC_WET_THRESHOLD:
    return 0.5
  # Linear ramp between wet boundary and nominal dry
  span = _FRIC_DEFAULT - _FRIC_WET_THRESHOLD
  ratio = (fric - _FRIC_WET_THRESHOLD) / span
  return 0.5 + 0.5 * ratio


def weather_cap_active(fric: float) -> bool:
  return fric < _FRIC_WET_THRESHOLD


def weather_speed_scale(fric: float) -> float:
  """
  When weather cap is active, derate speed: 1.0 at the boundary,
  down to 0.6 at the floor (heavy snow).
  """
  if fric >= _FRIC_WET_THRESHOLD:
    return 1.0
  if fric <= _FRIC_MIN:
    return 0.6
  span = _FRIC_WET_THRESHOLD - _FRIC_MIN
  ratio = (fric - _FRIC_MIN) / span
  return 0.6 + 0.4 * ratio
