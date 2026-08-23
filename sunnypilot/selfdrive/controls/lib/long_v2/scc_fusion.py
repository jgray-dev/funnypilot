"""FunnyPilot v3.6.2 — how much of SCC-M v2's cut is allowed through.

ONE FUSION, NOT TWO. Until v3.6.1 there were two: `fuse_map_target` gated OSM's
curve speeds behind the model, and `fuse_learned_target` gated a separately
learned speed behind visit count. SCC-M v2 produces a single cap whose corner
speed is already a blend of measured geometry and learned experience
(corner_speed.effective_a_lat), so there is one thing to gate and one place
that decides how far it is trusted.

────────────────────────────────────────────────────────────────────────────
WHY A CORROBORATION GATE STILL EXISTS AT ALL

The corner speed is ours now, but the ROAD SHAPE is still OSM's, and the
failure mode v3.5.9 documented has not gone away: where two lanes merge or one
splits, the way jogs sideways over a short distance, and any curvature
estimator run on that geometry sees a corner on a straight road.
road_geometry's turn-angle gate removes most of it — a junction jog turns
through a few degrees, not eighteen — but "most" is not "all", and the cost of
the residue is the car braking for nothing.

So an UNVISITED corner still has to be corroborated: by the model seeing
lateral action ahead, or, beyond the model's horizon where it has no opinion,
by proximity. That machinery is unchanged from v3.5.9 and its reasoning is in
the constants below.

────────────────────────────────────────────────────────────────────────────
WHY A VISITED CORNER BYPASSES IT

A learned corner is not a claim derived from OSM's geometry. It is a record
that THIS CAR went round THIS bend and either did or did not run out of
steering doing it. That evidence does not become weaker because the model has
not seen the bend yet, and it does not become weaker on a crest or in fog —
which is exactly when the model's silence is least informative and the
corroboration gate bites hardest.

`learned_conf` therefore does two things: it floors the corroboration (the same
shape v3.5.0's advisory floor had), and above LEARNED_TRUST_TH it also
suppresses the vision-disagreement veto outright. A corner we have measured
three times outranks the model's opinion that the road is straight.

Note what it does NOT do: it never widens MAP_SOLO_MAX_CUT. Trust buys
authority, not an unbounded slowdown.

Speed-domain only; the governor takes the min and the MPC + shaper own the
actual deceleration. Import-light (stdlib only).
"""
from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.curve_cap import CAP_INACTIVE

# Bounds on what a partially-corroborated cut may cost.
MAP_SOLO_MAX_CUT = 6.7   # m/s (~15 mph) — ceiling on a partially trusted cut
MAP_SOLO_MIN_CUT = 0.5   # m/s — below this the map is not saying anything useful

# FunnyPilot v3.5.6 — PROXIMITY IS AUTHORITY TOO, AND AUTHORITY BOUNDS THE CUT
# RATHER THAN SCALING IT.
#
# `allowed = cut * c` CONFLATES TWO QUESTIONS. `c` answers "is this corner
# real"; the envelope answers "how far under cruise should we be right now".
# Multiplying them means a SMALL early trim is multiplied down to nothing — and
# a small early trim is precisely what an early, progressive approach consists
# of. At c 0.3 a 1.2 m/s trim became 0.36, under MAP_SOLO_MIN_CUT, so it was
# dropped and the car did nothing. `min(cut, MAP_SOLO_MAX_CUT * c)` makes
# authority a CEILING; anything under it passes at full strength, and large
# cuts at middling corroboration get STRICTER, which is the right way round.
#
# A corner still on the map at 120 m is likelier real than one at 400 m, so
# distance is evidence. It is WEAKER evidence than the model agreeing, and the
# cap below 1.0 says so: full authority still needs vision or a learned record.
MAP_PROX_NONE_M = 400.0        # beyond this, distance grants nothing
MAP_PROX_FULL_M = 120.0        # at or inside this, the distance term saturates
MAP_PROX_MAX_AUTHORITY = 0.75  # distance alone may never grant FULL authority

# v3.5.9. The model's plan horizon in seconds — scc_vision_v2 reasons over ~8 s.
# Inside `v_ego * MODEL_HORIZON_T` the model has actually looked at the road, so
# its silence is informative and an unvisited corner must earn corroboration.
# Beyond it the model has no opinion and proximity may stand in. Multiplying by
# v_ego is what makes this the distance the model actually covers rather than a
# fixed number that is wrong at every speed but one.
MODEL_HORIZON_T = 8.0
# FunnyPilot v3.6.7 — 0.05 -> 0.30, AND THE VETO NOW OUTRANKS EVERYTHING.
#
# Owner: "SCC-V should be able to ENTIRELY override SCC-M. There's a lot of
# buggy or misleading map data in OpenStreetMap — for example where a one-lane
# road merges into two, the one-lane way just ends and the two-lane way's
# geometry folds onto itself. Vision shows zero desire to turn with almost full
# confidence, so SCC-M v2 should have NO authority to slow us down."
#
# TWO THINGS STOPPED THAT FROM HAPPENING, and both are fixed together because
# fixing either alone changes nothing.
#
# 1. THE THRESHOLD COULD NOT FIRE. `corroboration` is
#    `max(path curvature) * v^2 / (CORROB_FRAC * a_lat_target)`, i.e. the
#    lateral acceleration the model's PATH would pull at our speed, over
#    1.05 m/s^2. At 0.05 the veto needed the model to predict under
#    0.05 m/s^2 — a 13.7 km radius at 60 mph. `orientationRate.z` is a noisy
#    signal and the statistic is a MAX over the whole horizon, so ordinary
#    straight-road jitter clears that by a factor of two or three. The veto was
#    unreachable in practice as well as (per the note below) subsumed on paper.
#    0.30 is 0.32 m/s^2 — still nothing anyone would call a corner (2.3 km
#    radius at 60 mph, 550 m at 30) while sitting far above the noise floor. A
#    real 29 mph bend corroborates at 1.0.
#
# 2. A LEARNED RECORD BYPASSED IT. `c` was floored by `learned` BEFORE the veto
#    was evaluated, and one completed pass is 0.45 — so any bend SCC-M had ever
#    recorded ignored vision entirely. That is exactly backwards for the
#    reported case: the orphan learner (v3.6.5) will happily file a record at a
#    junction the car once struggled at, and that record then permanently
#    outranks a camera looking straight down an empty road. The veto is now
#    evaluated on the MODEL'S OWN number, before any flooring. Learning still
#    grants AUTHORITY where the model is not looking or already agrees — it
#    tells us how FAST a bend is, and it does not get to tell us one EXISTS
#    when the model can see that it does not.
#
# IT IS GATED ON SCC-V ACTUALLY RUNNING (`vision_available`), and that guard is
# load-bearing rather than defensive: `corroboration` is cleared to 0.0 in
# SCCVisionV2._reset(), so with the SCC-V toggle off or below its 5 m/s speed
# floor an ungated veto would silently disable SCC-M altogether.
VISION_DISAGREE_TH = 0.30   # below this the model is actively reporting "straight"

# FunnyPilot v3.6.2 — TWO PIECES OF ARITHMETIC, BOTH FOUND BY MUTATION TESTING,
# AND BOTH WORTH RECORDING BECAUSE THEY MAKE CODE BELOW LOOK LOAD-BEARING WHEN
# IT IS NOT.
#
# 1. THE v3.5.9 VETO CANNOT CHANGE ANY OUTPUT AT THESE CONSTANTS. A cut that
#    only just survives the veto's own threshold is `MAP_SOLO_MAX_CUT *
#    VISION_DISAGREE_TH` = 6.7 * 0.05 = 0.335 m/s, which is already under
#    MAP_SOLO_MIN_CUT (0.5) and therefore already dropped a few lines further
#    down. Deleting the veto entirely leaves the whole suite green, and it
#    leaves the CAR's behaviour unchanged too.
#
#    It is KEPT anyway, because it is the explicit statement of an intent that
#    the MIN_CUT check happens to satisfy by coincidence: a model that has
#    looked at the road and reports it straight should veto, not merely fail to
#    reach a threshold. `test_the_veto_is_currently_subsumed_by_min_cut` pins
#    the relationship, so lowering MIN_CUT or raising VISION_DISAGREE_TH makes
#    the veto start doing real work rather than silently removing a protection.
#
# 2. A LEARNED CORNER BYPASSES THE VETO VIA `max(c, learned)` BELOW, AND NEEDS
#    NOTHING ELSE. An earlier draft added `and learned < LEARNED_TRUST_TH` to
#    the veto condition, which reads like the bypass and is dead: `c` has
#    already been floored by `learned` when the veto is evaluated, and
#    `confidence_for(1)` is 0.45 — nine times VISION_DISAGREE_TH — so a single
#    completed pass clears the threshold by construction. The extra term could
#    never fire, and a constant that never fires is exactly the kind of dead
#    code that reads as a live knob.


def proximity_authority(dist_m: float) -> float:
  """[0, MAP_PROX_MAX_AUTHORITY] from distance to the governing corner."""
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
                    vision_corroboration: float, learned_conf: float = 0.0,
                    dist_m: float = 0.0, v_ego: float = 0.0,
                    vision_available: bool = False) -> float:
  """Return SCC-M v2's cap as the governor should see it.

  map_v_target: the smoothed cap (CAP_INACTIVE when it has nothing to say)
  v_cruise: the cruise speed the cut is measured against
  vision_is_active: SCC-V has independently latched a cap
  vision_corroboration: [0, 1], how much lateral action the model predicts
  learned_conf: [0, 1] confidence of the governing corner's learned record
  dist_m: distance to the governing corner (0 = unknown, grants nothing)
  v_ego: current speed, used to size the model's horizon (0 = unknown)
  """
  if not (map_v_target < CAP_INACTIVE):
    return CAP_INACTIVE

  if vision_is_active:
    return map_v_target

  c_model = min(max(float(vision_corroboration), 0.0), 1.0)
  c = c_model
  learned = min(max(float(learned_conf), 0.0), 1.0)
  # A learned record FLOORS corroboration; `max`, never assignment, so where
  # the model already agrees fully a learned corner changes nothing. Assigning
  # would look identical in review and would silently DOWNGRADE a corner the
  # model can see — a test pins the distinction.
  c = max(c, learned)

  d = float(dist_m) if dist_m and dist_m == dist_m else 0.0
  horizon = max(0.0, float(v_ego)) * MODEL_HORIZON_T if v_ego else 0.0
  model_could_see_it = 0.0 < d <= horizon
  # v3.6.7 — VISION IS SUPREME WHERE VISION IS LOOKING. Evaluated on `c_model`,
  # the model's OWN reading, deliberately before the learned floor above: a
  # record says how fast a bend is, not that one exists, and a camera looking
  # down an empty road is better evidence about existence than any map or any
  # journal line. Gated on SCC-V running, because an SCC-V that is switched off
  # reports 0.0 corroboration and would otherwise veto everything.
  if vision_available and model_could_see_it and c_model < VISION_DISAGREE_TH:
    return CAP_INACTIVE
  if not model_could_see_it:
    c = max(c, proximity_authority(d))
  if c <= 0.0:
    return CAP_INACTIVE

  cut = max(0.0, float(v_cruise) - float(map_v_target))
  allowed = min(cut, MAP_SOLO_MAX_CUT * c)
  if allowed < MAP_SOLO_MIN_CUT:
    return CAP_INACTIVE
  return float(v_cruise) - allowed
