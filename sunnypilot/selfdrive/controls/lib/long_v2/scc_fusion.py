"""FunnyPilot v3.4.9 — ONE Smart Cruise Control: map and vision are two views of one corner.

WHAT WAS THERE (v3.3.8). SCC-M's cap only reached the governor when SCC-V was
independently ACTIVE — a hard binary veto. The reasoning was sound: OSM curve
speeds are mistagged, rounded and stale often enough that letting the map brake
the car on its own produces slowdowns with no corner in front of them.

WHY IT UNDER-DELIVERED. The veto did not ask "does the model see a corner?", it
asked "has the model's own corner controller crossed its ACTIVATION threshold?"
— a much higher bar, and one the map can rarely clear at the moment it matters:

  * SCC-V's threshold is a comfort limit (a_lat_target). A corner that would
    pull 1.5 m/s^2 at our current speed is a real corner worth trimming for and
    is nowhere near activating vision.
  * The two look different distances ahead. SCC-M reasons out to 400 m; SCC-V
    reasons over the model's ~8 s plan, which at 30 m/s is ~240 m. The map's
    whole value is the early, gentle reduction, and for the entire early part
    of the approach vision has literally not seen the corner yet — so the veto
    was hardest exactly where the map was most useful.

Net effect on the road: corners that could have used a slowdown got none, which
is the reported symptom.

THE MERGE. Corroboration becomes CONTINUOUS instead of binary, and it scales
the map's AUTHORITY rather than switching it:

    vision ACTIVE            -> map passes through untouched (unchanged)
    vision sees a corner     -> map may take a FRACTION of the reduction it
                                asked for, proportional to how much lateral
                                action the model predicts at our current speed
    vision sees a straight   -> map is vetoed entirely (the v3.3.8 protection,
                                fully intact — this is the buggy-map-data case)

plus MAP_SOLO_MAX_CUT, so the worst a partially-corroborated map error can ever
cost is a bounded trim, never an arbitrary slowdown. A wrong map point on a
straight road still does nothing at all; a real corner now gets acted on well
before vision's own comfort threshold is crossed.

The corroboration signal itself is produced by SCC-V (see scc_vision_v2.py:
peak predicted lateral acceleration over the plan horizon evaluated at OUR
speed, normalised by a fraction of the comfort target, asymmetrically filtered
so it rises quickly and does not blink out mid-corner).

Speed-domain only; the governor takes the min and the MPC + shaper own the
actual deceleration. Import-light (stdlib only).
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE

# Fraction of a full cut the map may take per unit of corroboration is 1:1;
# these two bound the result.
MAP_SOLO_MAX_CUT = 6.7   # m/s (~15 mph) — ceiling on a partially-corroborated cut
MAP_SOLO_MIN_CUT = 0.5   # m/s — below this the map is not saying anything useful


def fuse_map_target(map_v_target: float, v_cruise: float, vision_is_active: bool,
                    vision_corroboration: float) -> float:
  """Return the map's cap as the governor should see it.

  map_v_target: SCC-M's smoothed cap (CAP_INACTIVE when it has nothing to say)
  v_cruise: the cruise speed the cut is measured against
  vision_is_active: SCC-V has independently latched a cap
  vision_corroboration: [0, 1], how much lateral action the model predicts
  """
  if not (map_v_target < CAP_INACTIVE):
    return CAP_INACTIVE

  if vision_is_active:
    return map_v_target

  c = min(max(float(vision_corroboration), 0.0), 1.0)
  if c <= 0.0:
    return CAP_INACTIVE

  cut = max(0.0, float(v_cruise) - float(map_v_target))
  allowed = min(cut * c, MAP_SOLO_MAX_CUT)
  if allowed < MAP_SOLO_MIN_CUT:
    return CAP_INACTIVE
  return float(v_cruise) - allowed
