"""Bounded, per-corner owner correction. Original learning is never erased."""
import math
import time
from openpilot.sunnypilot.feedback.protocol import CORNER_RULES, CORNER_CONTEXT, read_json, atomic_json

MAX_RULES = 200
MAX_LIFT_MS = 0.5
MAX_LIFT_FRAC = 0.03
PENALTY_RELIEF = 0.10
MATCH_METRES = 20.0
MATCH_DEGREES = 20.0


def matches(rule, lat, lon, bearing):
  try:
    distance = math.hypot((rule['lat'] - lat) * 111320,
                          (rule['lon'] - lon) * 111320 * math.cos(math.radians(lat)))
    angle = abs((rule['bearing'] - bearing + 180) % 360 - 180)
    return distance < MATCH_METRES and angle < MATCH_DEGREES
  except (KeyError, TypeError, ValueError):
    return False


def corrected_target(corner, cruise, rules):
  original = corner.v_target
  if corner.unmanageable or not math.isfinite(original) or not math.isfinite(cruise) or original <= 0 or cruise <= original:
    return original
  if any(matches(r, corner.lat, corner.lon, corner.bearing) for r in rules):
    # One fixed correction, not a ratchet: repeated reports cannot compound it.
    return original + min(MAX_LIFT_MS, MAX_LIFT_FRAC * original, PENALTY_RELIEF * (cruise - original))
  return original


class CornerFeedback:
  def __init__(self):
    self.rules = []
    self._read_at = 0.0
    self._write_at = 0.0

  def publish_context(self, controller, source, authority, long_active):
    now = time.monotonic()
    if now - self._write_at < .1:
      return
    self._write_at = now
    try:
      corner = next((c for c in controller.corners if abs(c.lat-controller.gov_lat) < 1e-7 and abs(c.lon-controller.gov_lon) < 1e-7), None)
      data = {'t':now, 'governing':bool(source == 3 and authority > .05 and long_active and corner is not None),
              'source':source, 'authority':authority, 'cap':controller.output_v_target}
      if corner is not None:
        data.update({k:getattr(corner, k) for k in ('lat','lon','bearing','radius','v_target','unmanageable')})
      atomic_json(CORNER_CONTEXT, data)
    except (OSError, ValueError, TypeError, AttributeError):
      pass  # diagnostics may never interrupt planning

  def update(self):
    now = time.monotonic()
    if now - self._read_at < .25:
      return
    self._read_at = now
    data = read_json(CORNER_RULES, {})
    # feedbackd republishes its durable rules after reboot. Missing/corrupt
    # state restores the original cap rather than creating a new exemption.
    self.rules = data.get('rules', [])[:MAX_RULES] if isinstance(data, dict) and isinstance(data.get('rules'), list) else []

  def target(self, corner, cruise):
    return corrected_target(corner, cruise, self.rules)
