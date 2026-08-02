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

# FunnyPilot v3.5.0 — a POSTED ADVISORY SPEED is corroboration in its own right.
# The whole reason vision gets a veto is that OSM curve speeds are computed and
# therefore wrong sometimes; an advisory limit is not computed, it is surveyed
# and signed. The model not having seen the bend yet does not make the sign
# fake, so an advisory floors the map's authority rather than replacing it —
# MAP_SOLO_MAX_CUT still bounds the result exactly as before.
ADVISORY_CORROB_FLOOR = 0.6

# FunnyPilot v3.5.6 — PROXIMITY IS AUTHORITY TOO, AND AUTHORITY BOUNDS THE CUT
# RATHER THAN SCALING IT. Two changes, and the second is the one that was
# actually holding the car back.
#
# 1. `allowed = cut * c` CONFLATES TWO DIFFERENT QUESTIONS. `c` answers "is this
#    corner real"; the envelope answers "how much should we be off cruise right
#    now". Multiplying them means a SMALL early trim gets multiplied down to
#    nothing — and a small early trim is precisely what an early, progressive
#    approach consists of. With `c` at 0.3 a 1.2 m/s trim became 0.36, under
#    MAP_SOLO_MIN_CUT, so it was dropped entirely and the car did nothing.
#    It is now `min(cut, MAP_SOLO_MAX_CUT * c)`: authority is a CEILING on how
#    much the map may take, and anything under that ceiling passes through at
#    full strength. Note this is also STRICTER than before for large cuts at
#    middling corroboration, which is the right way round.
#
# 2. Corroboration came only from the model, and the model's plan reaches about
#    240 m at 30 m/s while SCC-M reasons to 400 m. So for the whole early part
#    of an approach `c` was ~0 and the map was vetoed outright — exactly where
#    the map is the only thing that knows. v3.4.9's own docstring predicted
#    this; making corroboration continuous did not fix it, because 0 times
#    anything is still 0.
#
#    A corner that is still in the map at 120 m is far more likely to be real
#    than one at 400 m, so distance is evidence in its own right. It is WEAKER
#    evidence than the model agreeing, and it is capped below 1.0 to say so:
#    full authority still requires vision or a posted advisory. The honest cost
#    is that a mistagged map point near the car can now produce a bounded trim
#    where it previously produced none — bounded by MAP_SOLO_MAX_CUT *
#    MAP_PROX_MAX_AUTHORITY, about 11 mph, and only ever a speed cap.
MAP_PROX_NONE_M = 400.0        # beyond this, distance grants nothing
MAP_PROX_FULL_M = 120.0        # at or inside this, the distance term saturates
MAP_PROX_MAX_AUTHORITY = 0.75  # distance alone may never grant FULL authority


def proximity_authority(dist_m: float) -> float:
  """[0, MAP_PROX_MAX_AUTHORITY] from distance to the governing point."""
  try:
    d = float(dist_m)
  except (TypeError, ValueError):
    return 0.0
  if not d > 0.0 or d != d:
    return 0.0
  span = MAP_PROX_NONE_M - MAP_PROX_FULL_M
  f = (MAP_PROX_NONE_M - d) / span
  return min(max(f, 0.0), 1.0) * MAP_PROX_MAX_AUTHORITY


def fuse_map_target(map_v_target: float, v_cruise: float, vision_is_active: bool,
                    vision_corroboration: float, advisory_active: bool = False,
                    dist_m: float = 0.0) -> float:
  """Return the map's cap as the governor should see it.

  map_v_target: SCC-M's smoothed cap (CAP_INACTIVE when it has nothing to say)
  v_cruise: the cruise speed the cut is measured against
  vision_is_active: SCC-V has independently latched a cap
  vision_corroboration: [0, 1], how much lateral action the model predicts
  advisory_active: a posted advisory speed agrees there is something here
  dist_m: distance to the governing point (0 = unknown, grants nothing)
  """
  if not (map_v_target < CAP_INACTIVE):
    return CAP_INACTIVE

  if vision_is_active:
    return map_v_target

  c = min(max(float(vision_corroboration), 0.0), 1.0)
  if advisory_active:
    c = max(c, ADVISORY_CORROB_FLOOR)
  c = max(c, proximity_authority(dist_m))
  if c <= 0.0:
    return CAP_INACTIVE

  cut = max(0.0, float(v_cruise) - float(map_v_target))
  allowed = min(cut, MAP_SOLO_MAX_CUT * c)
  if allowed < MAP_SOLO_MIN_CUT:
    return CAP_INACTIVE
  return float(v_cruise) - allowed


# FunnyPilot v3.5.0 — the LEARNED corner map (see scc_learn.py).
#
# WHY THIS IS NOT JUST ANOTHER CALL TO fuse_map_target. The vision veto exists
# because OSM's curve speeds are COMPUTED FROM GEOMETRY BY SOMEONE ELSE. A
# learned point is not computed at all — it is a speed THIS CAR ACTUALLY WENT
# THROUGH THIS BEND, recorded only after a dip that recovered with no lead, no
# stop and no zone change (scc_learn.CornerObserver does that filtering). It is
# self-corroborating in the exact sense the map is not, so demanding the model
# also see the corner would throw away the one piece of evidence that is better
# than the model's.
#
# WHAT REPLACES THE VETO IS VISIT COUNT. A corner seen once is real evidence and
# gets a real but partial cut; three visits earn the full one. Vision agreeing
# can only ever raise that authority, never lower it.
LEARN_SOLO_MAX_CUT = 8.9   # m/s (~20 mph) — ceiling on a partly-trusted learned cut
LEARN_MIN_CUT = 0.5        # m/s — below this it is not worth a slowdown


def fuse_learned_target(learn_v_target: float, v_cruise: float, confidence: float,
                        vision_is_active: bool = False,
                        vision_corroboration: float = 0.0) -> float:
  """Return the learned cap as the governor should see it.

  learn_v_target: SCC-Learn's smoothed cap (CAP_INACTIVE when it has nothing)
  confidence: [0, 1] from visit count (scc_learn.confidence_for)
  vision_is_active / vision_corroboration: may only RAISE authority
  """
  if not (learn_v_target < CAP_INACTIVE):
    return CAP_INACTIVE

  if vision_is_active:
    return learn_v_target

  a = min(max(float(confidence), 0.0), 1.0)
  a = max(a, min(max(float(vision_corroboration), 0.0), 1.0))
  if a <= 0.0:
    return CAP_INACTIVE

  cut = max(0.0, float(v_cruise) - float(learn_v_target))
  allowed = min(cut * a, LEARN_SOLO_MAX_CUT)
  if allowed < LEARN_MIN_CUT:
    return CAP_INACTIVE
  return float(v_cruise) - allowed
