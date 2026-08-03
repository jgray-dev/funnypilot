FunnyPilot v3.5.8 (2026-08-02)
========================
Flash-time housekeeping, plus one security fix found while adding it.

1. THE FLASH PRUNES OLD BRANCHES AND DRIVE DATA
------------------------------------------------------------------------
`/api/flash` now, after a SUCCESSFUL checkout and before the reboot:

* deletes every local branch except those ending in `st` and the branch just
  flashed (`KEEP_BRANCH_SUFFIX`), and
* empties `/data/media/0/realdata` (`PURGE_DRIVE_DATA_ON_FLASH`).

WHY THE BRANCHES GO. They were kept as an offline unbrick path -- check out an
old version straight from local git objects with no network. That reason does
not survive contact with the device, as the owner pointed out: reaching the CLI
at all requires wifi or a hotspot, so anything that can run git can also fetch.
The path was never real, and it was holding 2.8 GB.

WHY THE DRIVE DATA GOES. 66 GB of route segments with no uploader and no
viewer. `PURGE_DRIVE_DATA_ON_FLASH` is a named constant precisely so it can be
flipped to False the day the Cloudflare-backed dashcam viewer exists -- at that
point the data has a reader and deleting it stops being tidying.

BOUNDED, AND THE BOUNDS ARE THE POINT:
* Both run inside the tail block, which the `&&` chain reaches only after
  `git reset --hard` succeeds. A FAILED FLASH DELETES NOTHING.
* The purge is `find ... -mindepth 1 -maxdepth 1 -exec rm -rf {} +`. `-mindepth
  1` keeps the directory itself, which loggerd expects to exist; `-exec ... +`
  avoids a glob that would blow ARG_MAX on tens of thousands of segments.
* The prune reads `refs/heads` only. Remote-tracking refs are how the device
  finds anything again afterwards.
* `sudo chown -R comma:comma /data/openpilot` was added at the end. The git
  calls here run under sudo and leave root-owned objects, which is what made
  three previous deploys abort half-way with "insufficient permission for
  adding an object to repository database".

2. THE BRANCH NAME REACHES A ROOT SHELL
------------------------------------------------------------------------
* fix: `branch` is interpolated into a command that runs as root, and the only
  validation was `startswith("funnypilot-")` -- which "funnypilot-;<anything>"
  passes. It is now also matched against `^[A-Za-z0-9._-]+$`. This was
  pre-existing; adding a second interpolation site is what surfaced it.

TESTS
------------------------------------------------------------------------
* 656 green, 0 failed. NEW `sunnypilot/navd/tests/test_flash_housekeeping.py`
  (40) -- every assertion is on the SOURCE, because there is no safe way to
  exercise `sudo rm -rf` in a test and the failure being guarded is an edit
  that looks reasonable in review. Mirrors the allow-list discipline
  `storage_cleanup.py` has had since v3.4.5.
* THREE guards MUTATION-TESTED: the injection regex removed, `-mindepth 1`
  dropped from the find, and the purge root widened to `/data/media`.
* PROCESS NOTE: the `-mindepth` guard PASSED its first mutation. It asserted
  `"-mindepth 1" in _TEXT`, and the COMMENT above the command contains that
  same phrase -- so the test was reading the documentation, not the code. It
  matches the command with a regex now. A test that can be satisfied by a
  comment is not a test.

FunnyPilot v3.5.7 (2026-08-02)
========================
The three missing v3.5.6 minimap tests, plus one diagnostic change aimed at a
reported reboot loop. READ THE REBOOT SECTION FIRST -- it does NOT contain a
fix, because the cause is not identified yet.

1. THE REBOOT LOOP IS NOT DIAGNOSED, AND THIS RELEASE DOES NOT FIX IT
------------------------------------------------------------------------
Reported: the device boots, stays up and online for about 45 s, then reboots
through the comma splash. Onroad and offroad alike. Not bricked, network fine.

WHAT THAT SYMPTOM MEANS. `manager_thread` kicks the AGNOS power-monitoring
watchdog once per loop by touching /var/tmp/power_watchdog. AGNOS power-cycles
the board when that file stops being touched. A device that runs for a fixed
interval and then reboots, in both states, is the signature of that watchdog
not being kicked -- which happens if EITHER

  * /var/tmp is not writable (a full or read-only data partition), or
  * `sm.all_checks(['deviceState'])` is failing (hardwared dead/slow/restarting)

and the whole thing was wrapped in `except Exception: pass`, so neither leaves
a trace.

* fix: the kick failure is now LOGGED -- `cloudlog.exception` on a raise, and a
  throttled `cloudlog.error` naming how many consecutive cycles have been
  missed. NOTHING ABOUT WHEN THE WATCHDOG IS KICKED HAS CHANGED. This only
  turns a silent reboot into a named one, so the next occurrence produces
  evidence instead of a guess.

WHY v3.5.6 IS NOT ACCUSED HERE. Everything v3.5.6 touched that runs offroad is
inert: four onroad-HUD modules whose changed code only executes while the
onroad screen is drawing, and whose module level gained nothing but constants
and two pure functions; plus two data tables in nav_webserver. The rest is
plannerd, which does not run offroad at all. That does not clear it, but it
does mean shipping a speculative "fix" would have been guessing, and this
project's history is unambiguous about where that leads.

FALSIFIABLE, and the next drive should settle it: if the log now shows "power
watchdog not kicked", read `df -h /data` and hardwared's state. If it does NOT
show up, the watchdog is being kicked and the reboot is coming from somewhere
else entirely -- power, thermal, or the updater -- and `LastManagerExitReason`
plus `dmesg` name which.

2. THE THREE MISSING v3.5.6 TESTS
------------------------------------------------------------------------
v3.5.6 shipped three minimap changes with no unit coverage, which was called
out at the time. They have it now.

* `TestZoneChange` (6) -- the boundary marker. The load-bearing case is
  `test_the_reference_zone_is_the_one_we_are_in`: the base zone must come from
  the first point AT OR AHEAD of the car, not from `pts[0]`, because BEHIND_M
  keeps ~170 m of trail and a boundary already driven through would otherwise
  be re-reported as one still ahead.
* `TestTintIsVisible` (4) -- that an ordinary 4-10 mph corner actually reads as
  a colour rather than as road, which is the reported "white 99% of the time".
* `TestTheTrailOutlivesTheScreen` (3) -- the tail arithmetic, asserted against
  the widget's real geometry rather than against the constant, so it stays true
  if EGO_FROM_BOTTOM or RANGE_M ever move.

* 616 green, 0 failed. All three MUTATION-TESTED fail-then-restore: the base
  zone taken from a point behind the car, DELTA_HI back to 25, and BEHIND_M
  back to 90.

FunnyPilot v3.5.6 (2026-08-02)
========================
Corner braking starts far earlier, the SLA arrow points at the button you
actually have to press, and the onroad HUD stops eating the frame budget.
NO NEW TESTS -- the existing suite is the regression harness for the
performance work, and it is unchanged except where behaviour was asked to
change.

1. SCC-M BRAKES FOR THE CORNER IT CAN ALREADY SEE
------------------------------------------------------------------------
Reported: the minimap shows the corner and its speed far ahead, but the car
does not slow until it is already in the bend.

TWO CAUSES, AND THE SECOND WAS DOING MOST OF THE DAMAGE.

(a) THE ENVELOPE ENGAGED TOO LATE. `sqrt(v_curve^2 + 2*a*d_eff)` with a flat
a = 1.0 m/s^2 sets ONE engagement distance for a given cut. For the reported
case (20 mph off at motorway speed) that is ~220 m, by which point the whole
reduction has to happen at once.

* feat: the budget is now INTEGRATED over distance-to-go, `v^2 = v_curve^2 +
  2*J(s)`, at 1.20 m/s^2 inside 60 m, 0.80 to 150 m and 0.50 beyond.

THE INTEGRAL IS THE LOAD-BEARING PART, not the numbers. Evaluating a(d) at the
point's own distance is the obvious way to write this and it is WRONG: the
resulting cap is not monotone in distance, so on some approaches it LOOSENS as
you close on the corner and the car speeds back up mid-approach. That was
caught by putting the table on screen, not by reading it. Integrating makes the
cap monotone by construction, and has the nicer property that the decel implied
at distance-to-go s is exactly a(s) -- so the schedule reads as the profile the
driver feels. For 29 -> 20 m/s:

    d = 400 m (1310 ft)   a 0.55   cap 30.0   above cruise; nothing yet
    d = 305 m (1000 ft)   a 0.66   cap 28.3   engages; coasting
    d = 200 m ( 660 ft)   a 0.79   cap 26.4
    d = 120 m ( 390 ft)   a 1.11   cap 24.0
    d =  40 m ( 130 ft)   a 1.20   cap 20.0   at corner speed, 2 s early

1.20 IS THE CEILING FOR A REASON, and it is not a comfort guess: this is a
SPEED cap, and long_mpc's CRUISE_MIN_ACCEL bounds what a falling cruise target
can command at -1.2. A steeper envelope is theatre -- the MPC cannot follow it.

(b) THE FUSION VETOED THE EARLY PART ENTIRELY, and this is why the envelope
alone would have changed nothing. `allowed = cut * corroboration` CONFLATES TWO
QUESTIONS: corroboration answers "is this corner real", the envelope answers
"how far under cruise should we be right now". Multiplying them means a SMALL
early trim is multiplied down to nothing -- and a small early trim is exactly
what a progressive approach consists of. At corroboration 0.3 a 1.2 m/s trim
became 0.36, under MAP_SOLO_MIN_CUT, so it was dropped and the car did nothing.

Worse, corroboration came only from the model, whose plan reaches ~240 m at
30 m/s while SCC-M reasons to 400 m. For the whole early approach it was ~0,
and zero times anything is still zero. v3.4.9's own docstring predicted this;
making corroboration continuous did not fix it.

* fix: `allowed = min(cut, MAP_SOLO_MAX_CUT * c)`. Authority is a CEILING on
  how much the map may take; anything under it passes through at full strength.
  Note this is also STRICTER than before for large cuts at middling
  corroboration, which is the right way round.
* feat: `proximity_authority()`. A corner still in the map at 120 m is far more
  likely to be real than one at 400 m, so distance is evidence in its own
  right. It is WEAKER evidence than the model agreeing and is capped at 0.75 to
  say so -- full authority still requires vision or a posted advisory.
  HONEST COST: a mistagged map point near the car can now produce a bounded
  trim where it previously produced none. Bounded at MAP_SOLO_MAX_CUT * 0.75,
  about 11 mph, and only ever as a speed cap.

2. THE SLA ARROW POINTS AT THE RIGHT BUTTON
------------------------------------------------------------------------
Reported: the up arrow shows when the driver has to press down.

* fix: `_chevron` was drawing `next_limit > cur_limit` -- the direction the
  UPCOMING ZONE is moving. That is a fact about the road, not an instruction.
  SLA's confirm is a press toward the CURRENT limit from wherever the SET SPEED
  is, which is what `_confirm_pressed` consumes: cluster < limit means press +.
  The two agree only by coincidence; 55 set in a 45 zone with a faster zone
  ahead produces exactly the reported wrong arrow. The chevron now asks the
  same question the state machine answers.

3. PERFORMANCE: THE ONROAD HUD GIVES THE FRAME BUDGET BACK
------------------------------------------------------------------------
Reported: the onroad camera view is laggy. Counted rather than guessed --
the minimap ribbon and the chrome gradient were together doing ~2100 cffi
calls per frame, roughly 4.3 ms of a 16.6 ms budget at 60 fps.

* perf: THE ROUTE IS DECIMATED at poll time. mapd spaces its points about a
  metre apart; at this widget's ~2 px/m that is a SUB-PIXEL segment, and the
  ribbon was drawing ~750 of them twice a frame. One point per 8 m gives a
  16 px segment -- 377 points become 56. A point is kept regardless if its
  speed or zone differs from the last kept one, so no colour transition is
  smeared: decimation must lose RESOLUTION, never INFORMATION.
* perf: the ribbon computes geometry and colour ONCE into a list and the two
  passes then only draw. `px()`, `edge_fade()`, `expected_speed_at()` and
  `ramp_color()` were all running twice per segment for a result that cannot
  differ between passes.
* perf: THE CHROME RINGS ARE CACHED. 85 nested outlines were each allocating a
  fresh `rl.Rectangle` and `rl.Color` every frame for a gradient whose inputs
  are constant most of the time. The key is the QUANTISED result -- alpha
  reaches the framebuffer as a byte anyway, so two float alphas that round the
  same are the same picture and must share an entry, or the easer's last few
  thousandths would miss forever.
* perf: loop-invariant trig hoisted out of the poll projection (1600
  transcendental calls per poll where 4 will do); the per-frame `import Mode`
  in `_draw_sign` memoised.

MEASURED: ~2136 -> ~276 cffi ops per frame for the ribbon and chrome together,
about 7.7x, freeing roughly 3.7 ms per frame.

WHAT WAS MEASURED AND THEN NOT DONE: throttling the three /dev/shm reads in
`_update_derived` from 60 Hz to their 20 Hz publish rate. Measured at 11.3 us
per read, so the whole saving is 1.35 ms per SECOND -- noise next to the above,
and it would have introduced a sampling delay for nothing. Recorded because
"we considered it and it was not worth it" is worth more than silence.

4. THE MINIMAP SAYS MORE, AND KEEPS THE ROAD IT HAS DRIVEN
------------------------------------------------------------------------
* fix: THE TINT RAMP WAS CALIBRATED FOR A DROP NOBODY EVER MAKES. Full red
  needed 25 mph under the expected speed, so an ordinary 8-10 mph corner sat at
  t = 0.2-0.3 and rendered as barely-tinted grey. Reported as "white 99% of the
  time", and it was: most of the ribbon is straight road at delta 0, and the
  corners that were not straight still had no colour to show. DELTA_HI is now
  13 mph -- a firm corner rather than an implausible one -- with the first
  coloured stop at t = 0.15. Measured against the old ramp: a 4 mph trim goes
  grey -> AMBER, 8 mph grey -> ORANGE, 10 mph amber -> RED. The neutral is also
  darkened, because it is the ROAD and it was competing with the white ego
  marker and the white text.
* feat: SPEED-LIMIT BOUNDARIES ARE DRAWN. The ribbon has carried the zone limit
  at every point since v3.5.2 (stored raw so the tint can be recomputed per
  frame) and nothing read it. `zone_change()` finds the first boundary ahead and
  draws a gate across the ribbon plus the new number, in THE SIGN'S OWN RED AND
  GREEN -- so the marker on the map and the halo on the sign are one statement
  about one event rather than two colour languages.
* fix: THE DRIVEN ROAD WAS BEING TRIMMED WHILE STILL ON SCREEN. Arithmetic, not
  taste: at this widget's 1.99 px/m the space below the ego marker is 184 px =
  92 m, and BEHIND_M was 90 -- five pixels short before any lag at all. Then the
  poll-time filter trims from the pose OF THAT INSTANT while the car keeps
  moving, and the displayed pose lags the polled one by POSE_TAU, which together
  cost another 81 px at 30 m/s. That is the ~150 px of vanishing tail.
  THE REMOVAL MUST BE DONE BY THE EDGE FADE, WHICH KNOWS WHERE THE SCREEN IS --
  not by a distance filter, which does not. BEHIND_M is now 170 m, overshooting
  the bottom edge by 155 px so the fade is the only thing that ever ends the
  ribbon. Decimation makes the extra tail almost free: about twenty points.

5. SCC-M BETWEEN BACK-TO-BACK CORNERS
------------------------------------------------------------------------
* fix: `CurveSpeedCap`'s release ceiling was `max(raw, v_cruise)`, and while the
  map is STILL CONSTRAINING that expression is just `v_cruise` -- so the cap
  climbed toward the full set speed even though the envelope for the NEXT corner
  said to stay down. It is now the raw envelope while a constraint exists. This
  can only ever LOWER the ceiling, so it needs no new safety argument.
* MEASURED, AND HONEST: this is a real hole but it is NOT the mechanism the
  driver hit. Simulated over 150-800 ft gaps between two 25 mph corners it moves
  the peak by at most 0.2 mph, because the release rate is slower than the
  envelope's own loosening and the `raw < value` branch takes over almost
  immediately. What actually kept SCC-M alive between corners is item 1(b) in
  this release: before it, corroboration collapsed on the short straight and the
  map was vetoed outright, which IS "accelerate back to the set speed".

TESTS
------------------------------------------------------------------------
* 603 green, 0 failed. NO NEW TESTS by request; the existing suite is what
  guarantees the performance pass changed nothing. NOTE that the minimap
  additions in item 4 therefore have NO unit coverage -- they are pure
  functions that would normally get some. They sit inside `safe_draw`, so the
  worst case is that widget disabling itself for the session rather than a UI
  crash, and the numbers above were checked by computing them rather than by
  eye. Worth adding tests for in the next version.
* THREE tests had expectations updated, all for the intentional SCC-M change:
  the envelope now uses J(), and authority is a ceiling rather than a scale.
  The graded-authority case gained the small-ask assertion that the old
  `cut * c` multiplied away -- the behaviour this release exists to restore.
* ON-ROAD VERIFICATION: (1) the car should begin easing off around 1000 ft from
  a corner the minimap is already showing, and the reduction should feel
  progressive rather than arriving at once; (2) the preActive arrow should
  always point at the button that actually confirms; (3) the camera view should
  be smooth again. If corners now slow for things that are not corners, the
  proximity authority is the first suspect and MAP_PROX_MAX_AUTHORITY is the
  knob -- not the envelope.

FunnyPilot v3.5.5 (2026-08-02)
========================
Three reported defects. The first is mine, from v3.5.4.

1. LEAD BRAKING: THE MPC NOW BELIEVES THE LEAD
------------------------------------------------------------------------
Reported: "slowing down for vehicles ahead feels wrong -- historically too
hard too late, and this one is the same impression but worse."

FIRST, THE REGRESSION, AND IT WAS MINE. v3.5.4 tapered `CP.stoppingDecelRate`
to 0.35x while the car was still rolling, to soften the last bite of brake at
the end of a stop. It shipped with a safety argument that was true and
IRRELEVANT: "the ramp starts from last_output_accel and only ever adds more on
top". In the `stopping` state THE PID IS RESET AND PRODUCES NOTHING, so that
ramp is the only brake authority the car has -- slowing it does not soften an
extra bite, it slows the completion of the whole stop.

The numbers were never checked against THIS car, which is what makes it a
defect rather than a taste call. Upstream's default `stoppingDecelRate` is 0.8
m/s^3; the K5 takes sunnypilot's Hyundai DEFAULT config, which is 0.40. Walking
from 0 to `stopAccel` -2.0 takes 5 s at full rate and 14 s at 0.35x. The
stopping state became very nearly a HOLD, with the deficit repaid at full rate
only below 0.5 m/s: soft, then a grab.

* fix: the taper is REVERTED. `test_stopping_ramp_runs_at_the_cars_full_rate`
  now pins the per-frame step to `CP.stoppingDecelRate` exactly.
* GENERALIZED: a rate limiter is only "just a comfort scale" when something
  else owns the target. Check what else is driving before you slow one down.

SECOND, THE PART THAT WAS ALWAYS WRONG. The MPC turns a moving lead into a
stationary obstacle by adding the lead's own stopping distance to its position,
and it computed that distance with OUR comfort deceleration:

    obstacle = x_lead + v_lead^2 / (2 * COMFORT_BRAKE)

That asserts the lead will decelerate at 2.2 m/s^2 no matter what the lead is
observably doing. A real stop is 3-5 m/s^2, so the term over-estimates how far
the lead will travel, the obstacle is placed too far away, and we start braking
too late -- then need MORE than COMFORT_BRAKE to recover. The shortfall is
arithmetic, not opinion:

    v_ego = v_lead = 20 m/s, lead braking at 4.0 m/s^2, t_follow 1.6 s
      lead actually travels    400 / (2*4.0) = 50.0 m
      we need to stop in       400 / (2*2.2) = 90.9 m, plus STOP_DISTANCE 7.5
      -> the gap we must already have is           48.4 m
      what the old constant asked for is           39.5 m

* feat: NEW `selfdrive/controls/lib/lead_physics.py` (stdlib-only, because
  long_mpc.py imports acados and cannot be constructed off-device).
  `decel = clip(-a_lead, COMFORT_BRAKE, LEAD_DECEL_MAX)`.

BOUNDED SO IT CAN ONLY EVER HELP, WHICH IS THE WHOLE SAFETY ARGUMENT. Floored
at COMFORT_BRAKE, so a lead that is coasting, holding speed, or easing off more
gently than we would returns the BIT-IDENTICAL old number -- "not constantly
braking when following a lead vehicle simply slowing down" is preserved by
construction, not by tuning. A bigger believed decel only ever moves the
obstacle CLOSER, so no input makes this brake later than v3.5.4 did. Capped at
4.0 not for safety but for NOISE: `aLeadK` is a Kalman output on a radar track,
and an uncapped spike would yank the obstacle tens of metres closer for one
frame -- the brake jab this change exists to remove. A short filter, seeded at
COMFORT_BRAKE and reset on lead loss, does the rest.

THIRD, a smaller one from v3.5.3. `JERK_DOWN_V` put 3.0 at a demand of 0.0,
which slewed every demand between -1.0 and 0 more slowly than before -- and
that band is exactly "ease off for a lead that is slowing". The intent was only
ever to soften a THROTTLE LIFT, so the relaxation now starts at 0 and the whole
demand <= 0 half of the table is bit-identical to pre-v3.5.3. A lift at +1.0
still gets 2.5.

2. THE MINIMAP RUNS THROUGH THE CAR
------------------------------------------------------------------------
Reported: the position marker sits to the right of the road line, and sometimes
there is a gap between the marker and the road ahead.

* fix: NEW `lateral_offset_at_ego()`. The offset is real and the geometry was
  honest -- mapd's points are the OSM way, i.e. the road CENTRELINE, while the
  GPS fix is the car, in the right-hand lane. But this map is about the road
  AHEAD and its speeds; lane position is not information here, it is the one
  thing stopping the widget reading as "the road I am on". The ribbon is
  shifted laterally to pass through the marker. TRANSLATION ONLY -- no
  rotation, no per-point warping, so every curve and distance is untouched.
  Interpolated at fwd == 0 rather than snapped to the nearest point, because
  snapping steps every time the nearest index changes, which is precisely the
  1 Hz jitter v3.5.1 removed. Mutation-tested.
* fix: NEW `stitch_to_ego()`. mapd publishes the route from its matched
  position forward, so after a re-match the first point can be tens of metres
  ahead with nothing joining it to the car. The ribbon is stitched back to the
  origin, carrying the first point's speed and zone so the join is tinted like
  the road it leads to.
* BOTH ARE BOUNDED (`LANE_SHIFT_MAX_M` 12, `STITCH_MAX_M` 60), and the bound is
  the safety property: a bad route match must be allowed to LOOK wrong rather
  than drag the ribbon somewhere it does not belong, or invent geometry the
  controller cannot see. Both bounds mutation-tested.

3. SLA COULD NOT BE RE-ENABLED
------------------------------------------------------------------------
Reported: "I can see the indicator by the speed limit, but changing speed to
enable it does nothing" -- cleared only by toggling the setting off and on.

THE DEFECT: `preActive` is a 6 s window, and the only doors into it were a ZONE
CHANGE or the first limit of the drive. Both are events the DRIVER DOES NOT
CONTROL. Miss the window once and there was no gesture that could reopen it;
toggling the setting worked only because it routes through `disabled`, which
re-arms.

WHY A LEAD MAKES IT LIKELY, which is the clue that found it -- the driver
noticed it cleared once the car ahead turned off. Braking for a lead
disengages, and re-engaging opens the window at the exact moment the driver is
busy with the car in front. Behind a lead you also sit with a set speed BELOW
the limit, so the arrow asks for `+`, and `+` is the one press you will not
make while closing on someone. Either way the window expires unconfirmed.

* fix: a recent cruise button event while inactive REOPENS the window. It
  cannot activate anything by itself -- `_enter_pre_active` clears the pending
  releases, so the confirming press is still a second, DIRECTIONAL one. All
  this restores is the driver's ability to ask. Mutation-tested, and
  `test_reopening_does_not_by_itself_activate` pins the half that matters:
  activation ADOPTS the set speed, so a stray tap silently locking SLA on at
  whatever the cluster reads is the failure mode to avoid.

TESTS
------------------------------------------------------------------------
* 603 green, 0 failed. NEW `test_lead_physics.py` (24), plus 12 minimap and
  6 SLA cases.
* SIX guards MUTATION-TESTED fail-then-restore: a hard-braking lead no longer
  believed, the noise cap removed, the v3.5.4 stop taper reintroduced, the SLA
  re-arm branch deleted (reproduces the reported bug verbatim), the lane offset
  snapping instead of interpolating, and the stitch bound removed.
* PROCESS NOTE: the first run of the lead-physics mutation PASSED, and the
  guard was fine -- the `sed` had four-space indentation and this repo uses
  two, so nothing was mutated at all. A mutation test that does not visibly
  break something proves NOTHING; check the mutation applied before believing
  the result.
* ON-ROAD VERIFICATION REQUIRED: (1) braking for a lead that is stopping should
  begin noticeably earlier and stay lighter; (2) following a lead that is
  merely slowing should feel IDENTICAL to v3.5.4 -- if it does not, the floor
  in `believed_lead_decel` is not doing its job and that is a bug, not a tuning
  question; (3) coming to rest should be back to the v3.5.3 feel.

FunnyPilot v3.5.4 (2026-08-01)
========================
Three comfort changes, all longitudinal, all off-device testable, plus three
onroad visual refinements. Zero schema changes, zero new params, zero new
assets -- this branch cannot trigger a device rebuild.

1. TURN LIMITING IS NOW ANTICIPATORY
------------------------------------------------------------------------
`limit_accel_in_turns` saw a corner only through the MEASURED steering angle,
which makes it REACTIVE: the accel ceiling came down once the wheel was already
turned, i.e. once you were in the bend -- precisely when a change in
longitudinal accel is least welcome. You accelerated up to turn-in and then got
backed off mid-corner.

* feat: NEW `selfdrive/controls/lib/turn_limit.py` (import-light) with
  `predicted_lat_accel()` and the moved `limit_accel_in_turns()`.

THE TRAP, AND IT IS THE SAME ONE v3.4.9 FOUND IN scc_vision_v2. The obvious
quantity is `orientationRate.z * velocity.x` -- the lateral accel the MODEL
intends to pull. But the model PLANS TO SLOW for corners, so that product reads
as "nothing to do" exactly where there is something to do. The fix is to
recover the pure geometry,

    curvature = orientation_rate_z / velocity_x        [rad/m]

which is a property of the ROAD and contains no intent at all, and evaluate it
at OUR speed: `a_lat = curvature * v_ego^2`. Mutation-tested.

SAFETY POSTURE. The predicted term joins the measured one by `max()`, never
replacing it, so this can only ever be MORE conservative than before. Every
failure path returns 0.0, which makes the feature a no-op and restores the old
numbers bit-for-bit. It bounds the accel CEILING only and can never command
braking -- `test_the_braking_floor_is_never_touched` pins that. The lookahead
is deliberately short (2.5 s): the point is to stop accelerating INTO a bend
about to arrive, not to hold the car back for one 200 m away.

SCOPE, HONESTLY: with the fork's 70% A_CRUISE_MAX table the requested accel is
often already below the turn limit, so this only bites in real corners --
roughly a_lat above 1.5 m/s^2 at city speeds and 2.0 at 56 mph. It will not
change how the car feels on a motorway sweeper.

2. THE END OF A STOP IS TAPERED
------------------------------------------------------------------------
The `stopping` state walks accel down toward `CP.stopAccel` at a constant
`stoppingDecelRate`. At full rate that last bite of brake pressure arrives
while the car is still rolling, and that is the nod you feel as you come to
rest. The rate is now scaled by speed, restoring the full rate below 0.5 m/s so
the brake hold is always secured firmly.

THIS CANNOT MAKE THE CAR STOP LATER THAN COMMANDED: the ramp starts from
`last_output_accel`, which is already whatever the planner asked for, and only
ever adds MORE braking on top. `test_stopping_ramp_only_ever_adds_braking`
pins it, and the taper is bounded to <= 1.0 so it can never amplify.

3. LAUNCH IS GENTLE, BRAKE RELEASE IS NOT
------------------------------------------------------------------------
`STARTING_ACCEL_RATE` was one constant for two completely different jobs done
back to back:

  * while the output is NEGATIVE it is RELEASING THE BRAKE. That must stay
    brisk -- slowing it is a car that sits at a green light.
  * once the output is POSITIVE it is APPLYING LAUNCH TORQUE. That is where the
    head-snap lives, and it is the half worth softening.

The rate is now interpolated on the current accel (6.0 -> 2.5 m/s^3 across
-0.5 -> +0.5 m/s^2), which keeps the rate itself continuous so there is no kink
where the two jobs meet. Mutation-tested against a flipped schedule, which
would give a gentle brake release and a snappy launch -- exactly backwards, and
a plausible-looking edit.

4. THE SCREEN HAS ONE EASING, AND EVERYTHING USES IT
------------------------------------------------------------------------
Almost every state on this HUD was a CUT. The engagement glow snapped between
grey, cyan and green; the long-status dot cut between green and red, which on a
solid disc is the most visually violent transition on the screen; the sign halo
jumped the instant a zone was confirmed or lost, and since WEIGHT is the halo's
whole message, a step in weight is a step in the message.

* feat: NEW `Eased` / `EasedColor` in `hud/tokens.py`. First-order ease with
  ONE house time constant, `EASE_TAU = 0.18 s`.

WHY IT IS A PRIMITIVE AND NOT THREE LOCAL LERPS. The point of a design system
is that unrelated things move alike; three hand-rolled fades drift the moment
either is touched. `hud_renderer` owns the glow and dot easers, `SpeedSign`
owns the halo's, and all three read the same tau.

THREE THINGS IN IT ARE LOAD-BEARING, none of them taste:

  * `exp(-dt / tau)`, NOT a fixed per-frame fraction. A fraction makes the feel
    a function of frame rate, so it changes when the device is hot and
    throttling -- a bug that only appears in the conditions you can least
    reproduce. Mutation-tested.
  * `EASE_SNAP` so a value actually REACHES its target. An asymptote leaves a
    pill parked at 99% alpha forever.
  * `_EASE_DT_MAX = 0.25 s`, so a stalled or backgrounded frame does not
    teleport the value and undo the whole point.

The easers must be stepped EXACTLY ONCE per frame -- they read the clock
themselves, so a second call in the same frame sees dt ~= 0 and silently halves
the rate. `SpeedSign.render` carries a comment saying so at the call site.

5. RADIUS IS A SCALE, AND THE LIGHT COMES FROM ONE PLACE
------------------------------------------------------------------------
Corner radii were picked per widget, so a pill, a plate and a chip were three
unrelated shapes. They are now three steps of one scale (`R_CHIP`, `R_PLATE`,
`R_PILL`) chosen by the widget's SIZE, which is what makes a set of surfaces
read as one material.

`plate()` also gained a single top highlight line (`SPECULAR`). One implied
light source from above is the cheapest thing that makes a flat scrim read as a
surface rather than as a hole punched in the image -- and it is one extra
`draw_line_ex`, not a gradient or a texture.

COLLAPSED TO ONE PALETTE. `speed_sign.py` had declared RED/GREEN/CYAN as
byte-identical copies of three tokens; `tokens.py` exists precisely to stop
that, and it had already happened once. They are aliases now, so the halo turns
the same red as the long-status dot and the same green as the engagement glow.
A driver should learn one red, not two.

* test: an AST guard -- `speed_sign.py` may not construct `rl.Color` with a
  literal argument at all. VALUE equality alone would pass if someone re-typed
  the same hex, so the guard checks the SOURCE: the drift is the problem, not
  the current value. Mutation-tested by pasting a byte-identical local copy
  back in; both palette tests fail on it.

6. THE CHROME FOLLOWS THE SCENE
------------------------------------------------------------------------
The vignette and horizon bands were tuned for daylight and applied at full
strength regardless. At night the camera image is already dark, so a 72%
vignette plus a 350 px 80% top scrim is far heavier than the scene needs and
eats road view for nothing.

`chrome_scale()` reads `deviceState.screenBrightnessPercent`, which hardwared
already drives from the ambient light sensor and which `ui_state` already
subscribes to -- no new signal, no new param.

FLOORED WELL ABOVE ZERO, AND THAT IS THE WHOLE DESIGN. v3.5.0 established that
the vignette is LOAD-BEARING for the state glow: a glow drawn straight onto a
bright sky washes out completely, which is exactly when you most want to know
whether the car is steering. Scaling the vignette to nothing at night would
trade one failure for another, so `CHROME_MIN = 0.55` is a constraint and not a
tuning knob. Mutation-tested against removing the floor. Garbage input returns
1.0 -- full chrome is the daylight-safe answer, so an unreadable sensor
degrades to exactly today's behaviour rather than guessing "dark".

TESTS
------------------------------------------------------------------------
* 565 green, 0 failed. NEW `test_turn_limit.py` (14), six longcontrol cases,
  and 20 UI cases (`TestEased`, `TestChromeScale`, `TestLongDotColor`,
  `TestOnePalette`).
* EIGHT guards MUTATION-TESTED: evaluating at the model's speed instead of
  ours, the predicted term replacing rather than tightening the measured one,
  the lookahead window removed, the starting schedule flipped, a stop taper
  that amplifies instead of softening, a fixed per-frame ease fraction, the
  chrome floor removed, and a local colour re-declaration.
* PROCESS NOTE: the first version of the max() guard PASSED its mutation,
  because it compared two calls that the mutant moved together. Relative
  assertions are worthless against a mutation that shifts both sides -- it is
  now an absolute one. Watch for that shape.
* ON-ROAD VERIFICATION: (1) the car should stop adding speed slightly before
  turn-in rather than backing off mid-corner; (2) coming to rest should have
  less of a final nod; (3) pulling away from a light should start just as
  promptly but build more gently. If (3) feels SLOW TO MOVE, that is the brake
  release and NOT this change -- the schedule keeps full rate there; look at
  CP.stopAccel instead. (4) NONE OF THE VISUAL WORK CAN BE TESTED OFF-DEVICE --
  no GL context, no camera. Logic is unit-tested and the import path is
  guarded, but the LOOK has to be judged on the car. If a widget vanishes
  on-road, grep the log for "onroad hud widget ... disabled": safe_draw names
  the failure exactly.

FunnyPilot v3.5.3 (2026-08-01)
========================
Two comfort changes, both small, both off-device testable. Nothing else.

1. THE ACCEL CEILING NO LONGER SURVIVES A DISENGAGEMENT
------------------------------------------------------------------------
A defect, not a tuning opinion. `prev_accel_clip` feeds a +/-0.05-per-frame
rate limiter on the acceleration CEILING. That limiter exists to stop the
ceiling stepping WHILE ENGAGED -- but the `reset_state` branch reset
`v_desired_filter`, `a_desired`, the shaper and `lead_grace`, and never this.

Across a disengagement there is no continuity worth preserving, so the stale
value just throttles you on the way back in. The ceiling has to walk up at
1.0 m/s^2 per second:

  * disengage mid-corner, where turn limiting has pulled the ceiling to ~0.1
  * or during an SLA gas gate, which pins it to coast accel -- NEGATIVE on a
    downhill
  * drive manually, re-engage on a straight
  * and the car will not accelerate for one to two seconds

Same input, different response depending on invisible history, which is the
definition of unpredictable. It is also one of the open suspects the v3.4.8
post-mortem left for the reported "~10 s coast".

The reset can only ever WIDEN the ceiling on the first engaged frame, never
narrow it, and it touches nothing while engaged.

2. LIFTING OFF THE THROTTLE IS NO LONGER A BRAKE APPLY
------------------------------------------------------------------------
`jerk_down` interpolates on the demanded accel, and `np.interp` CLAMPS outside
its breakpoints. With the old two-point table

    JERK_DOWN_BP = [-3.5, -1.0] -> [12.0, 4.0]

EVERY target above -1.0 got 4.0 m/s^3 -- including simply lifting off at +1.0
with nothing wrong, which took the car from full throttle to zero in a quarter
of a second. That is the most-felt harshness on an ordinary highway mile, and
it was an artefact of the clamp rather than a decision.

    JERK_DOWN_BP = [-3.5, -1.0, 0.0, 1.0] -> [12.0, 4.0, 3.0, 2.5]

THIS CANNOT WEAKEN BRAKING, and the reason is the interpolation variable: the
lookup is on the DEMAND, not on the current output. The moment the planner asks
for -2.0 the table returns ~9.5 on that very frame, whatever the shaper was
doing before. The relaxed values are reachable only while the demand is mild.
`test_a_hard_demand_is_unaffected_by_the_new_breakpoints` pins exactly that.

TESTS
------------------------------------------------------------------------
* 525 green, 0 failed. NEW: monotonicity of the jerk table, throttle-lift vs
  brake-apply, hard-demand-unaffected, and an AST guard that `prev_accel_clip`
  is reset in the `reset_state` branch (asserted structurally because
  longitudinal_planner imports the acados MPC and cannot be built off-device --
  same technique as the cruise_ext single-writer and scc_learn no-syscall
  guards).
* Three guards MUTATION-TESTED: the reset reverted, the table returned to two
  points, and a mis-ordered table that would make hard braking gentler than
  light braking -- the one thing long_shaping promises it cannot do, and the
  one that would look like a harmless tuning edit in review.
* ON-ROAD VERIFICATION: (1) re-engage after a manual corner should now pull
  away immediately rather than after a beat; (2) throttle lift-offs should feel
  rounded. If braking feels AT ALL softer, that is not this change -- the
  demand-side lookup makes it impossible -- and the next suspect is
  COMFORT_BRAKE in long_mpc.py.

FunnyPilot v3.5.2 (2026-08-01)
========================
Minimap only. Everything else in v3.5.1 is byte-identical.

1. THE TINT NOW MEANS SOMETHING
------------------------------------------------------------------------
It used to be measured against the POSTED LIMIT:

    delta = limit_there - map_target_velocity_there

which answers the wrong question. A 35 mph curve in a 55 zone glowed red even
when your set speed was 45 -- the map was shouting about a 20 mph drop you were
never going to take.

It is now measured against THE SPEED WE EXPECT TO BE DOING AT THAT POINT:

    expected = min(set speed, zone limit there x (1 + SLA offset) if SLA on)
    delta    = expected - map_target_velocity_there

* Set speed 45 into a 35 curve is a 10 mph drop and reads amber, not red.
* If the bend sits in a slower zone that SLA will have walked you down into by
  the time you arrive, the comparison already happens AT THE REDUCED SPEED --
  so the colour shows the drop you will actually feel, not the one from here.
* `min()`, never `max()`: SLA can only be one more thing lowering the ceiling,
  and a driver's carried +20% offset must not raise the expected speed above a
  set speed they deliberately chose. Mutation-tested, along with the zone term
  and the ratio floor that stops a nonsense offset inverting the speed.
* The reference is the SET SPEED when cruise is set, otherwise the current
  speed -- nothing is holding you to anything else when cruise is off. Kept in
  m/s throughout: `self.set_speed` has already been through the base renderer's
  display-unit conversion, and mixing that with mapd's m/s velocities is
  exactly the kind of unit error that looks plausible on screen.
* Route points are now stored RAW (velocity + zone limit) rather than as a
  baked-in delta, because the comparison depends on live values that change
  every frame while the poll is 1 Hz.

2. NO CONTAINER, FULL HEIGHT, AND YOU ARE HERE
------------------------------------------------------------------------
* The plate is gone. What makes a thin ribbon legible is contrast AT the
  ribbon, not a rectangle behind it, so each segment is stroked twice: a wider
  dark transparent pass, then the colour. That backdrop follows the road
  instead of framing it. Drawn as TWO PASSES OVER THE WHOLE RIBBON, not two
  strokes per segment -- per-segment ordering lets the next halo paint over the
  previous colour at every joint, which reads as a dashed line.
* The strip is now full screen height down the right side, 240 px wide, still
  24 px clear of the dev-UI column. Range raised 300 -> 400 m, matching
  `scc_map_v2`'s own lookahead exactly, so the map shows precisely the horizon
  SCC-M reasons over. Checked with arithmetic, not by eye (the v3.5.0 lesson):
  ego sits 184 px off the bottom at ~2.0 px/m, giving 92 m of travelled road
  behind and 60 m of lateral half-width.
* NEW ego marker. The old bare white triangle did not read as the car's
  position because nothing distinguished it from the route itself. It is now a
  dark-haloed disc AT the exact projection origin with a heading wedge above
  it: the disc says where, the wedge says which way, the halo separates both
  from the ribbon underneath.
* "ADV" and "NO FIX" carry their own shadows now -- with the plate gone there
  is nothing behind them but road.

TESTS
------------------------------------------------------------------------
* 521 green, 0 failed. NEW `TestExpectedSpeedAt` (8) including both reported
  cases verbatim.
* Three guards MUTATION-TESTED: min()->max() (the SLA offset raising the
  reference above set speed), the zone term dropped entirely, and the ratio
  floor removed so a bad offset inverts the expected speed.
* ON-DEVICE VERIFICATION REQUIRED: none of the drawing runs off-device.

FunnyPilot v3.5.1 (2026-08-01)
========================
First stable cut of the v3.5 UI, after a drive. Onroad refinements from that
drive plus ONE REAL DEFECT: a spurious "TAKE CONTROL IMMEDIATELY" for a car
that never put a wheel wrong.

0. HOTFIX: THE FIRST PUSH OF THIS BRANCH DID NOT BOOT
------------------------------------------------------------------------
    Manager failed to start
    TypeError: unsupported operand type(s) for |: 'function' and 'NoneType'
    torque_bar.py:151  warm_color: rl.Color | None = None

pyray's `rl.Color` is a cffi FACTORY FUNCTION, not a Python type, so the union
evaluates `function.__or__(None)` when the `def` runs -- at import -- and
raises. That kills the UI module, which kills manager.

THE RULE THIS CORRECTS. v3.4.2 recorded "never put a capnp type in a `|`
union". That wording was too narrow and is why this happened again: capnp was
just the first library to bite us. The rule is **no non-type may be a direct
operand of `|` in an evaluated annotation**, from any library.

pyray is the worst case because THE NAME TELLS YOU NOTHING: `rl.Rectangle` is
a real cdata class and unions fine (upstream `system/ui/widgets/__init__.py`
has shipped it forever), `rl.Color` is a function and does not.

* fix: the two parameters are unannotated, with a comment saying why -- the
  same treatment cruise_ext.py got after v3.4.2.
* `sunnypilot/tests/test_capnp_annotations.py` now scans pyray too. Banned by
  default; `PYRAY_TYPE_ALLOWLIST = {Rectangle}` is opt-in and may only be
  extended after verifying a name ON A DEVICE. Mutation-tested by pasting the
  exact failing line back in.
* WHY NOTHING CAUGHT IT, which is the part worth remembering: the local AST
  guard in `test_hud_imports.py` only scanned the `hud/` package, and
  torque_bar.py lives under `selfdrive/ui/mici/`. And no RUNTIME test could
  ever have caught it -- the UI suite stubs pyray, and a MagicMock supports `|`
  perfectly happily. Off-device, a static scan is the only instrument that
  works.

1. THE COMMS ALERT -- diagnosed, and it was ours
------------------------------------------------------------------------
Reported: one full-screen orange "Communication Issue between Processes" with
the chime, mid-drive, while the software kept driving perfectly throughout.

WHAT THAT ALERT ACTUALLY MEANS. selfdrived marks a service dead when nothing
has arrived for 10x its period (`cereal/messaging/__init__.py`: `alive[s] =
(cur_time - recv_time[s]) < 10./frequency`). `longitudinalPlan` is 20 Hz, so
the budget is 500 ms. Missing it once raises `commIssue`, which is a
SOFT_DISABLE -- the full-screen prompt -- and the moment plannerd catches up
the alert clears and control continues. "Alarming and self-clearing, with no
loss of control" is precisely the signature of a planner frame that ran long,
not of a process that died.

WHAT RAN LONG. v3.5.0's SCC-Learn store called `maybe_flush()` from
`update_targets`, i.e. from plannerd's 20 Hz loop, and that did real IO on
/data: `os.path.getsize`, `os.statvfs`, `os.makedirs`, then an append. loggerd
is writing megabytes to the same eMMC beside us. Any one of those can block for
hundreds of milliseconds when the device is busy, and 500 ms is all it takes.
The flush period was also exactly 60 s -- the same as loggerd's segment
rotation -- so the two beat against each other and coincided occasionally,
which is a good description of "it happened once on that drive".

THE RULE THIS LEAVES BEHIND: **nothing in a 20 Hz control-loop process may
touch /data.** Not "nothing slow" -- nothing at all, because on a shared eMMC
you do not get to know which call is the slow one.

* fix: every write in `long_v2/scc_learn_store.py` now happens on a SHORT-LIVED
  daemon thread handed a finished list of lines. Short-lived rather than a
  long-running worker with a queue, deliberately: it owns no shared mutable
  state (so no lock and no race with the planner mutating `corners`), at most
  one exists at a time (so a stalled disk cannot pile threads up), and it holds
  one bounded list and then dies -- which is what the v3.4.6 rule asks for
  (bounded in MEMORY, not merely in time). A dropped flush costs at most one
  drive of learning, the same price every other failure path here already pays.
* `maybe_flush` now makes NO SYSCALLS AT ALL. The journal-size check reads a
  tracked `_journal_bytes` counter instead of `stat`ing the file.
  `test_the_flush_path_makes_no_syscalls` pins this on the AST, because the
  natural way to write this function is the way that caused the bug.
* The startup compaction (`_rewrite`) is off-thread too. It is the biggest
  write the module ever makes; the READ stays synchronous because the planner
  needs the data before it can do anything with it.
* `FLUSH_S` 60 -> 47 s, so nothing here is phase-locked to loggerd. Belt and
  braces given the thread, and free.
* FALSIFIABLE: if the alert recurs, `cloudlog.event("commIssue", ...)` in
  selfdrived names the exact service in `not_alive`. If it is not
  `longitudinalPlan`/`longitudinalPlanSP`, this diagnosis is wrong and the
  planner is exonerated -- do not tune anything here for it.

2. ONROAD REFINEMENTS
------------------------------------------------------------------------
* feat(sign): the upcoming limit and the SLA percentage moved to a column to
  the RIGHT of the current sign, tab above sign. The station is now one sign
  tall instead of three deep, so it stops growing down into the road view, and
  "here is the limit, here is what is changing about it" reads as one object.
* fix(minimap): the ribbon no longer jitters. The cause was not the drawing --
  the source (`LastGPSPosition`) updates at 1 Hz, so the ego pose the route was
  projected from changed in one step per second and the whole ribbon snapped
  with it. Polling faster cannot help; there is no new data to read. The route
  is now kept in raw lat/lon and re-projected EVERY FRAME from a pose that
  eases toward each new fix (`POSE_TAU` 0.35 s), with a `POSE_SNAP_M` guard so
  a GPS relock snaps instead of dragging the map across the box for a second.
  `bearing_lerp` takes the short way round, or a wrap past north would spin the
  route through 358 degrees.
* fix(minimap): the road already driven fades out of the box instead of
  vanishing at its edge. `edge_fade` eases alpha to zero over the last 26 px,
  which also replaces the old hard `inside()` test -- and that matters because
  there is no scissor available here (nesting one un-clips every widget drawn
  after it, see v3.5.0). `BEHIND_M` keeps 60 m of travelled road so there is
  something to fade.
* fix(glow): the state glow is even on all four edges. It was four full-span
  gradients, so the top and left overlapped in the corner and composited twice
  -- and because the overlap is `depth` square while the rails are thousands of
  pixels long, the eye read it as the rails fading unevenly toward the corners
  rather than as a bright corner. It is now nested rectangle outlines: every
  pixel belongs to exactly one ring and its alpha is a function of distance
  from the nearest edge, which is the definition of an even falloff.
* feat(speed): the unit label is gone. It never changes on a given car, so it
  carried no information -- and sitting beside the number it pushed the number
  off centre by half its width, breaking alignment with the road name above and
  the status pills below. The hero speed is now centred on the same axis as
  everything else in that column.
* feat: the longitudinal state dot moved to the bottom-left corner with a 20 px
  margin, and still clears the dev-UI bottom rail when that is on.
* feat(torque bar): quieter and fixed-height. `TorqueBar` gained `opacity`,
  `grow`, `warm_color` and `hot_color`, all defaulting to the previous
  behaviour so the mici HUD is untouched. The fork passes `grow=False` -- the
  height ramp made the bar grow INTO the road view exactly when the driver is
  looking through it, and it duplicated what the colour already said. The
  colour ramp starts earlier (0.60 rather than 0.75) since it is now the only
  channel, and uses the HUD's own amber/red rather than its private palette.
* feat: no driver-monitoring face. DM is disabled on this fork (24 h timeouts,
  and selfdrived does not subscribe to `driverMonitoringState`), so the widget
  was drawing a readout for a system that cannot act. The renderer is still
  CONSTRUCTED -- that is what keeps driverStateV2 flowing -- only the draw is
  skipped under the sunnypilot UI.
* feat(alerts): informational banners are suppressed. The design rule for this
  HUD is that state is implied by something already on screen: the set speed is
  visible in the set-speed station, a lane change in the turn-signal chevrons,
  SLA's intent in the halo on the sign. A banner for those covers the road to
  say something the screen is already saying, and it trains the driver to
  ignore banners -- which is what you cannot afford when a real one arrives.
  THE FILTER IS ON `alertStatus`, NOT ON A LIST OF EVENT NAMES: `normal` is
  openpilot's own word for "nothing is wrong", so new upstream events are
  classified correctly without anyone remembering to update a list here.
  `AlertSize.full` is always shown whatever its status.

TESTS
------------------------------------------------------------------------
* 506 green, 0 failed. NEW: `bearing_lerp` and `edge_fade` cases in
  `test_hud_logic.py`; `test_the_flush_path_makes_no_syscalls` in
  `test_scc_learn.py`.
* ON-DEVICE VERIFICATION REQUIRED for every visual change above -- none of the
  drawing can be exercised off-device. Geometry was checked with arithmetic
  (the v3.5.0 lesson), not by eye.

FunnyPilot v3.5.0e (2026-08-01)
========================
The onroad UI, rebuilt on one design system; advisory speed limits wired into
SCC-M; SCC-Learn, a corner map the car builds by driving; the experimental-mode
wheel button removed. NO SCHEMA AND NO COMPILED FILE IS TOUCHED -- everything
here is Python and raylib, so nothing rebuilds on the device.

1. SCC-M NOW READS POSTED ADVISORY SPEEDS
------------------------------------------------------------------------
* feat(SCC-M): `MapAdvisoryLimit` / `NextMapAdvisoryLimit` (OSM
  `maxspeed:advisory`) have been sitting in /dev/shm/params unread since mapd
  shipped. They are a different KIND of signal from everything else SCC-M has:
  `MapTargetVelocities` is geometry mapd COMPUTED, an advisory speed is one a
  highway engineer SURVEYED AND SIGNED. That makes it the best available answer
  to "is this corner real" -- the exact question the vision veto exists to ask.

  Used in two bounded ways, and deliberately NOT as a target:
    - AS A FLOOR ON THE CAP, **ONLY WHERE GEOMETRY IS ALREADY CONSTRAINING**.
      Where mapd's curve math has found a bend but under-rates it, the advisory
      pulls the cap down -- but only to `advisory * ADVISORY_MARGIN` (1.15, the
      signs are conservative) and never more than ADVISORY_MAX_CUT (~20 mph)
      below cruise. `min()` is the only operator it touches, so it can never
      raise a cap geometry already set.
      THE GEOMETRY PRECONDITION IS LOAD-BEARING and was missing from the first
      cut of this feature. An advisory tag belongs to a WAY, not a point, so a
      curvy road carries one along its whole length; folding it in wherever it
      exists holds the car at `advisory * MARGIN` along every straight between
      the bends -- a sustained slowdown with no corner in sight. The unguarded
      `min()` reads as obviously correct, which is why it is now pinned by
      `test_an_advisory_alone_never_slows_the_car`.
    - AS CORROBORATION. `advisory_active` floors the map's authority in
      scc_fusion at ADVISORY_CORROB_FLOOR. A surveyed advisory does not stop
      being evidence because the model has not seen the bend yet. It is a
      FLOOR, not an override: where vision already corroborates fully the
      advisory changes nothing, and MAP_SOLO_MAX_CUT still bounds the result.

  WHY NOT A TARGET: advisory tags are per-WAY, so a curvy road carries one for
  its whole length. Obeying it literally would hold the car down through every
  straight between the bends. Geometry still decides the SHAPE of the slowdown.

* feat: NEW `long_v2/scc_shm.py`. plannerd publishes the governing point --
  `argmin(v_allowed)`, the one point SCC-M is braking for -- plus how much of
  its requested cut survived the fusion. The onroad minimap draws that instead
  of re-deriving it: two copies of a selection rule drift the moment either is
  tuned, and a debug readout that disagrees with the controller is worse than
  none. Same /dev/shm pattern as sla_shm, same staleness contract (a wedged
  plannerd reads as "no constraint", never as a stuck one).

2. THE ONROAD UI
------------------------------------------------------------------------
* feat(ui): NEW `selfdrive/ui/sunnypilot/onroad/hud/` -- tokens, chrome,
  speed_sign, route_map, stations. Every visual constant now lives in one
  module instead of beside the code that draws it. Before this the screen had
  eleven widgets and eleven visual languages (three corner radii, four
  unrelated greens, plates on some elements and bare text on others).

* feat(ui): STATE GLOW REPLACES THE SOLID BORDER. Engagement is now an inward
  bloom from the frame edge -- 60% alpha at the border, linear to zero over
  120 px, all four edges -- instead of a 30 px coloured ring. It reads in
  peripheral vision and it can BREATHE, which is how driver override is now
  communicated instead of by a banner.
  THE VIGNETTE IS LOAD-BEARING, not decoration: a glow drawn onto a bright sky
  washes out completely, which would make the indicator untrustworthy in
  exactly the conditions where you most want it. A darkening pass is drawn
  FIRST, deeper (220 px) and stronger (72%), so the glow always has a dark
  ground. Order is the whole trick: vignette, glow, bands, content.

* feat(ui): HORIZON BANDS. Chrome lives in a 350 px top scrim and a 138 px
  bottom scrim -- derived from what the camera actually shows -- and the middle
  third is never drawn into. Stations: set speed + sign column top-left, road
  name / speed / status pills top-centre, route minimap top-right, accel spine
  and long-state dot on the left edge, diagnostics along the bottom.
  EVERY STATION RESERVES ITS SPACE whether or not it has content. That is not
  cosmetic: the dev-UI rail computed `gap_width = (available - total) / gaps`
  over a CONDITIONALLY built list, so the moment `liveDelay` dropped validity
  every remaining metric slid sideways.

* feat(ui): THE SPEED-LIMIT SIGN CARRIES EVERY SLA STATE WITH NO TEXT. One halo
  around the sign: COLOUR says which way the limit is about to move (red lower,
  green higher, cyan SLA-active-and-satisfied), WEIGHT says how close --
  thickness and bloom grow continuously from a hairline at 400 m to a solid
  ring at the boundary. The upcoming limit slides in beneath at 62% scale, so
  "65 now, 45 soon" reads as two objects rather than a sentence. The MUTCD and
  Vienna faces are unchanged; they are legally recognisable iconography and a
  restyled speed-limit sign is a worse speed-limit sign.
  The preActive arrow PNGs are replaced by a chevron drawn from two lines --
  one less asset that can fail to load.

* feat(ui): THE SCC-M ROUTE MINIMAP. `MapTargetVelocities` drawn ego-centric,
  bearing-up, 300 m range, each segment tinted by how far BELOW THE POSTED
  LIMIT the map wants you there -- so a 35 mph curve in a 35 zone stays neutral
  and a 35 mph curve in a 55 zone glows. A ring marks the governing point:
  solid = the fusion passed the cut through at full authority, hollow = the
  corroboration is scaling it because the model has not seen the corner yet.
  NO SIDE ROADS, and that is a finding rather than a shortfall: mapd publishes
  exactly eleven params and none contain junctions or surrounding geometry.
  Drawing them would have meant querying the offline OSM database on the UI
  thread AND showing geometry the controller cannot see -- the opposite of a
  debug tool. The map is one road, which is also why it stays minimal.

* feat(ui): THE WHEEL BUTTON IS GONE. Tapping it toggled `ExperimentalMode`
  mid-drive; the mode now comes solely from the offroad setting. Nothing about
  how the car BEHAVES changed -- selfdrived has always read the param and
  published `selfdriveState.experimentalMode`. An E2E pill in the status strip
  keeps the mode visible, and the top-right slot it vacated is where the
  minimap went.

* chore(ui): DELETED `smart_cruise_control.py`, `speed_renderer.py` and
  `rocket_fuel.py` (onroad). Their information moved into the status strip, the
  hero speed and the accel spine respectively; all three were left with zero
  importers.

3. SCC-LEARN: THE CORNER MAP THIS CAR BUILDS BY DRIVING
------------------------------------------------------------------------
SCC-M asks OSM how fast a bend can be taken and is wrong often enough that it
needs a vision veto to be safe. But the car already knows the answer for any
road it has driven: the speed it actually went through the bend. Record that,
keyed by position AND heading, and after one pass a road has a corner map that
owes nothing to OSM's geometry -- and unlike OSM it is derived from this car,
this driver and this tyre set. On a commute it is strictly better information
than the map. No cloud, no sync, no account: it is a file on this device.

* feat: NEW `long_v2/scc_learn_store.py` -- persistence and the geometry index.
  NEW `long_v2/scc_learn.py` -- the observer and the governor. They are separate
  modules because the hard part (deciding what is worth remembering) has to be
  testable without a car, and the persistence has to be testable without a
  planner.

WHAT IS WORTH REMEMBERING, which is the whole safety story.
Recording "the car slowed down here" is easy and useless -- the car slows for
traffic, lights, stop signs, junctions, zone changes and lead vehicles, and
learning any of those builds a map that brakes at a green light forever. The
observer commits only for a shape specific to a bend:

    A LOCAL MINIMUM IN SPEED, FOLLOWED BY RECOVERY, WITH NOTHING ELSE
    EXPLAINING IT.

THE RECOVERY REQUIREMENT IS THE LOAD-BEARING PART, and it is the general lesson
of this feature: a corner is transient -- you slow, you turn, you speed back
up. A stop sign, a light and congestion all end in a stop or a long hold, so
requiring the car to come back UP before committing rejects all three WITHOUT
THE SYSTEM NEEDING TO KNOW THEY EXIST. Everything else is explicit exclusion,
and each one names a thing that would otherwise be learned as a corner: a lead
seen at any point, v_min below MIN_CORNER_V (a junction or a queue), the posted
limit changing mid-dip (SLA owns that), SLA ramping or gas-gating, standstill,
a dip longer than MAX_DIP_S (congestion) or shorter than MIN_DIP_S (noise), and
GPS accuracy worse than MAX_GPS_ACC_M -- a record keyed on a position we are
not sure of is WORSE than no record, because it caps the car where no bend is.

* The record lands at the APEX (where v_min occurred), not where the dip began.
  Braking is planned TO the corner, so a record at the entry would ask the next
  pass to be at corner speed a hundred metres early.
* It stores `v_min * LEARN_MARGIN`, not v_min. The observed minimum already
  contains whatever margin the driver or SCC-V chose; capping AT it and then
  observing again would compound the margin every visit until the car crawled.

STORAGE, AND THE EVICTION RULE THAT WAS THE EXPLICIT REQUIREMENT.
* Cell key is (lat_cell, lon_cell, heading_octant) at ~22 m. The octant is in
  the KEY: a bend taken northbound and the same tarmac southbound are different
  approaches with different entry points and different carryable speeds.
* A coarse ~1.1 km index maps to the fine keys inside it, so a lookup probes 9
  coarse cells instead of scanning 25,000 records. At 20 Hz that difference is
  the whole feasibility of the feature.
* MERGE ON WRITE (`MERGE_M` 30 m, `MERGE_BEARING_DEG` 45): a grid has edges and
  a bend is as likely to sit on one as anywhere else. Without this, two visits
  to the SAME corner landing either side of a boundary become two records with
  one visit each -- which is not merely untidy, because VISIT COUNT IS WHAT
  EVICTION KEEPS. A boundary corner on the commute would be filed as two
  roads-driven-once and thrown away first. Found by a test, not by inspection.
* EVICTION: over MAX_RECORDS the store is sorted by visit count FIRST and
  last-seen second, and the tail is dropped -- the road you take to work every
  morning outlives the road you drove once on holiday. Mutation-tested, because
  sorting by recency instead looks identical in review and is exactly backwards.
* PERSISTENCE IS AN APPEND-ONLY JOURNAL, COMPACTED AT STARTUP. plannerd is
  `only_onroad` so it restarts every drive, which gives compaction a free
  moment and means NO BACKGROUND THREAD IS EVER NEEDED. That is deliberate:
  v3.4.5 shipped a startup task that shelled out to `git gc` and OOM-killed the
  device, and the rule that came out of it (see the v3.4.6 post-mortem) is that
  anything running beside a moving car must be bounded in MEMORY, not merely in
  time. Appending a few short lines every FLUSH_S is bounded by construction; a
  full rewrite happens once, before the car moves. Hitting MAX_JOURNAL_BYTES
  mid-drive STOPS WRITING rather than compacting -- it costs one drive of
  learning and never risks a multi-megabyte rewrite next to a moving car.
  `MAX_JOURNAL_LINES` bounds the startup read explicitly rather than trusting
  the byte cap to imply a bound.
* `MIN_FREE_BYTES` (512 MB) sits above `deleter.py`'s floor, so this never
  competes with drive logs for the last of the disk. Every operation is
  best-effort: a full disk, a corrupt line, a truncated write or a read-only
  filesystem all degrade to "no learned data", and losing the file entirely is
  an acceptable outcome -- the feature simply relearns.
* The estimate rises fast and falls slow (ALPHA_UP 0.5 / ALPHA_DOWN 0.2). A cap
  can only ever SLOW the car, so learning to brake HARDER is the change that
  deserves more evidence.
* A PASS WE OURSELVES GOVERNED CANNOT RAISE THE ESTIMATE (`FLAG_SELF` ->
  `allow_raise=False`). Without this the feature reinforces itself: our cap
  sets v_min, v_min comes back in at +LEARN_MARGIN above the cap, ALPHA_UP
  adopts half of it, and the estimate ratchets ~2.5% per visit -- measured at
  16.8 -> 22.0 m/s (37 -> 49 mph) over eleven commutes, i.e. the feature
  quietly stops working on exactly the roads it exists for. Learning to go
  SLOWER from such a pass is still real information and is still allowed.
* THE OBSERVER READS `carState.vEgo`, NOT the planner's `v_ego`. What the
  planner passes around is `v_desired_filter.x`, which it integrates forward
  with the plan's accel and only corrects toward vEgo with a 2 s time constant
  -- so while engaged it is the speed we INTEND, and while disengaged
  `reset_state` pins it to vEgo exactly. Learning off that would record the
  same corner from a different signal depending on whether openpilot happened
  to be on.
* A dip closes at `v_min + RECOVER_MS`, still well below the pre-corner
  reference, so the observer clears that reference on commit. Carrying it
  across would satisfy the entry condition on the very next frame and open a
  second dip on the way OUT of the same bend; only MIN_DIP_S stood between that
  and a duplicate record. Measured on a deep, short dip with a slow exit: a
  spurious second record 3 s past the apex, 1.6 m/s high.

TURNING THE MAP BACK INTO A CAP.
* `SCCLearnV1` reuses SCC-M's exact envelope maths (`sqrt(v^2 + 2*a*d_eff)`,
  arriving early by ARRIVAL_LEAD_T) so a learned corner and a mapped corner
  produce the same SHAPE of slowdown and the governor's `min()` compares like
  with like. Speed-domain only, like every other governor here: the MPC and the
  shaper own the actual deceleration and it can never command an acceleration.
* NEW `fuse_learned_target()` in scc_fusion.py, and it is deliberately NOT a
  second call to `fuse_map_target`. THE VISION VETO EXISTS BECAUSE OSM'S CURVE
  SPEEDS ARE COMPUTED FROM GEOMETRY BY SOMEONE ELSE. A learned point is not
  computed at all -- it is a speed THIS CAR ACTUALLY WENT THROUGH THIS BEND,
  after the observer above rejected everything that was not a bend. It is
  self-corroborating in the exact sense the map is not, so demanding the model
  also see the corner would throw away the one piece of evidence that is better
  than the model's.
* WHAT REPLACES THE VETO IS VISIT COUNT. `confidence_for()` gives 0.45 at one
  sighting and 1.0 at three, and that scales how much of the requested cut
  reaches the governor -- bounded by LEARN_SOLO_MAX_CUT (~20 mph) so a single
  bad observation can never produce an arbitrary slowdown. Vision agreeing can
  only ever RAISE that authority, never lower it (`max()`, mutation-tested --
  assigning instead looks identical in review).
* `nearby()` filters on AHEAD-NESS via a dot product, not a radius: without it
  the bend you have just exited keeps braking you on the way out.
* Reports as `sccMap` in `longitudinalPlanSource`. It IS a map, just one we
  made, and the capnp enum has no room for a new member on this fork -- a
  schema change forces a device rebuild (v3.4.1 post-mortem).

* feat(ui): "LRN" pill in the status strip -- lit while a learned corner is
  governing, otherwise showing how many corners are known. An empty store reads
  as "nothing learned yet", not as a missing feature. Fed by a NEW
  `/dev/shm/fp_learn` channel with the same staleness contract as fp_scc. A
  SEPARATE FILE rather than two more fields on fp_scc, deliberately: the
  minimap reader unpacks fp_scc positionally and its contract is pinned by
  tests, and widening a working channel for an unrelated feature is how a
  reader that indexes [4] starts reading a different quantity.
* SCC-Learn SHARES SCC-M's `SmartCruiseControlMap` TOGGLE rather than adding a
  param of its own -- registering one edits `common/params_keys.h`, which is
  C++ and compiles. Sharing also gives the feature an on-device off switch on
  its first flash, which a hard-coded True would not. The toggle gates the CAP
  only; learning keeps running with it off, so the map is there the moment it
  goes back on.
* feat(SLA): new read-only `SpeedLimitAssist.busy` property (mid-ramp or
  gas-gating). The observer refuses to learn a dip that a zone boundary
  explains -- without it there would be a permanent corner cap at every
  speed-limit sign on the commute.

4. NOT CRASHING THE DEVICE
------------------------------------------------------------------------
This was the highest-priority constraint and it shaped the architecture.
`selfdrive/ui/ui.py` draws BOTH screens from one process, so a raise in an
onroad widget kills the UI, manager restarts it, and it dies again -- a boot
loop, on a device whose settings screen is how you would flash your way out.

* Every module in `hud/` imports only pyray, the stdlib, and modules already on
  the UI's import path. Nothing touches the filesystem, /dev/shm or a texture
  at import or in a constructor -- the minimap's Params handle is created
  lazily on first onroad draw, because /dev/shm/params does not exist offroad.
* `tokens.safe_draw` wraps every new widget. The first exception logs a
  traceback and disables THAT WIDGET for the session. One broken readout costs
  you the readout, never the screen. No retry, no re-enable: a widget raising
  at 60 Hz would burn the frame budget the rest of the UI needs.
* NEW `test_hud_imports.py` really imports every hud/ module with only the
  graphics deps stubbed, AST-scans for capnp-in-`|`-union (the v3.4.0/v3.4.1
  brick), and pins that nothing does IO at import. The capnp lookups that
  remain are inside function bodies for the same reason.
* NO NEW PARAMS. Registering one means editing `common/params_keys.h`, which is
  C++ and would compile. The minimap is gated on SCC-M being enabled and the
  accel spine on the existing RocketFuel toggle.
* NO NEW ASSETS, NO SHADERS, NO SCHEMA CHANGES. Everything is drawn from
  raylib primitives that already appear elsewhere in this repo, so every call
  shape is one the device has executed before.

* fix(ui): the minimap does NOT open a scissor region. AugmentedRoadView
  already has one around the content rect and raylib's EndScissorMode DISABLES
  the test rather than restoring an outer region -- nesting one would silently
  un-clip every widget drawn after it on that frame. Out-of-box segments are
  dropped instead.
* The ego marker uses `draw_triangle_fan` with plain (x, y) tuples, matching
  the proven call shape in onroad/model_renderer.py, and sidestepping
  `draw_triangle`'s winding-order sensitivity (wrong order draws nothing).

TESTS
------------------------------------------------------------------------
* 497 green, 0 failed. NEW `test_hud_imports.py` (15), `test_hud_logic.py`
  (45), `test_scc_advisory.py` (24), `test_scc_learn.py` (73).
* FIFTEEN guards MUTATION-TESTED fail-then-restore. From the advisory work:
  advisory raising instead of lowering the cap, an unbounded advisory cut,
  advisory overriding rather than flooring corroboration, the scc_shm staleness
  check, safe_draw not disabling a failed widget, the minimap's rotation with
  sin/cos swapped (which mirrors every corner and looks plausible), and a halo
  that stops tracking distance. From SCC-Learn: eviction sorted by recency
  instead of visit count, the estimate falling as fast as it rises, the
  cell-edge merge removed, committing without requiring recovery (5 tests fail
  -- this is the one that turns every red light into a corner), a lead no
  longer poisoning a dip, one visit trusted like ten, corners behind us still
  capping, corroboration overriding confidence instead of flooring it, and an
  unbounded learned cut.
* ruff clean across selfdrive/ sunnypilot/ system/ common/.
* ON-DEVICE VERIFICATION REQUIRED: none of the drawing can be tested off-device
  (no GL context, no camera). The logic is unit-tested and the import path is
  guarded; the LOOK has to be judged on the car.
* ON-ROAD VERIFICATION REQUIRED / FALSIFIABLE (SCC-Learn): drive a known road
  and watch the LRN count. If it climbs on a straight road, an exclusion is
  leaking and the observer is the bug -- do NOT retune MIN_DROP_MS to hide it.
  If a corner is learned but never caps on the second pass, the first suspects
  are the ahead-ness dot product and the heading tolerance in `nearby()`, not
  the confidence schedule. If it caps somewhere no corner is, read the flags on
  that record: FLAG_DRIVER alone means a driver slowdown was learned and the
  exclusion set needs another member.

FunnyPilot v3.4.9 (2026-07-31)
========================
Four requested changes -- more lateral interpolation without the old EMA's
phase cost, a scheduled handback after a driver steering intervention, two SLA
defects around the predictive ramp, and the SCC-V/SCC-M challenge system merged
into one feature -- plus a dead-code sweep that takes the suite to zero known
failures and ruff to zero warnings.

1. LATERAL: MORE INTERPOLATION, PAID FOR BY THE PLAN INSTEAD OF BY LAG
------------------------------------------------------------------------
* feat(lat): NEW `selfdrive/controls/lib/knot_filter.py`. `lat_smooth.py` can
  only shape the path BETWEEN 20 Hz knots; the knot SEQUENCE still delivered
  every rate change whole inside one 50 ms period, and that step is what is
  felt in the car. The v3.2.12 attempt to spend the lagd window on this was an
  EMA, whose implicit process model is "the curvature stays where it is" — so
  it lagged EVERYTHING, including entirely predictable turn-in, by its time
  constant, with no bound on how far the command had drifted. That is the
  "sloppy / the car is between the two places the model wanted" report, and it
  was reverted in v3.3.2.
  KnotFilter's process model is THE MODEL'S OWN PUBLISHED PLAN. controlsd
  already samples the plan one model step past the action horizon
  (`_model_lookahead_curv`, used since v3.3.6 to aim the spline's exit slope);
  that sample is by construction a PREDICTION of the next frame's action — the
  action is the plan at `lat_delay + DT_MDL` and the lookahead is the same plan
  at `lat_delay + 2*DT_MDL`, i.e. the same absolute instant. So only the
  INNOVATION (raw action minus what the previous plan said it would be) is
  damped. MEASURED, not asserted:
    - perfect prediction (steady turn-in, holding a curve, a scheduled unwind):
      output is BIT-IDENTICAL to the raw action. Zero phase lag on predicted
      motion — the property an EMA cannot have.
    - an UNPREDICTED step at the model's own rate rail is delivered
      45/32/13/6/2/1% over six model frames instead of 100% in one: the peak
      frame-to-frame command change drops to 45%.
    - pure jitter: frame-to-frame command change stdev drops to 52% of raw.
    - a fully BLIND plan on a sustained rail-rate maneuver (the degenerate
      worst case) settles 0.104 m/s^2 behind = 42 ms, UNDER ONE MODEL FRAME.
    - `DEV_MAX_LAT_ACCEL` = 0.15 m/s^2 hard-caps |command - model desire| in
      lateral acceleration. Max reachable deviation measured at 0.135, so the
      cap is a real backstop for a WRONG prediction, not the operating point.
      Even pinned at the cap for a whole convergence the path error is ~3 mm —
      "the car ends up somewhere the model did not want" is now a number.
    - a genuinely large surprise (several times what the model's own limiter
      can produce) passes through with beta == 1, i.e. UNTOUCHED. The v3.3.2
      requirement that onsets stay decisive is intact.
  Knot values and knot TIMES are unchanged; the filtered value is handed to
  LatSmoother exactly as the raw action was, and clip_curvature still enforces
  the ISO limits downstream. Deviation is published as a fifth field on
  `/dev/shm/lat_interp` (appended; the dev-UI reader indexes defensively).

2. LATERAL: THE HANDBACK IS A RAMP, AND THE GAP SETS ITS LENGTH
------------------------------------------------------------------------
* fix(lat): NEW `selfdrive/controls/lib/lat_handback.py`. REPORTED CYCLE, once
  per corner: the model asks for more turn than the driver wants -> the driver
  holds the wheel out -> the override softening cuts torque to 60% -> the
  driver settles the car and relaxes -> softening releases in ~0.15 s WITH THE
  MODEL'S DESIRE UNCHANGED and a frozen integrator still holding pre-override
  wind-up -> the wheel bites -> the driver grabs it again.
  v3.2.8's OverrideGate fixed the wrong half. Its dwell hysteresis stopped the
  softening CHATTERING against wheel inertia, and that still works and is
  reused verbatim. Nothing scheduled the RETURN: it was the same near-step
  whether the controller was 0.1 or 3 m/s^2 away from what the driver had just
  established, and the size of that step IS the bite.
  The return is now a smoothstep RAMP whose duration is interpolated from the
  desired-vs-measured lateral-accel DIVERGENCE at release: 1.6 s when they are
  close (the corner case — nothing to correct urgently, so no reason to snatch)
  down to 0.45 s when they are far apart (evasive — dawdling off the model's
  path is the wrong trade). The divergence is peak-held with a bleed across the
  press, because in the reported scenario the driver has ALIGNED the car by the
  time they relax, so an instantaneous sample would schedule the wrong ramp.
  For a corner-sized gap the per-frame authority step is under half of what the
  old 0.15 s release delivered on its FIRST frame.
* fix(lat): the integrator is the other half of the bite. It was FROZEN while
  the driver pressed, so it still held whatever it wound up to before the
  intervention. It is now BLED (1 s time constant) while the driver is actually
  in charge, and stays frozen through the first half of the return ramp, so the
  authority coming back is feedforward plus a live proportional term rather
  than a stored one.
* Unchanged: the driver always wins physically (panda driver-torque limits and
  the EPS governor untouched), this only ever scales the request DOWN, and with
  no intervention it is an exact no-op (scale 1.0 every frame, asserted).

3. SLA: THE UP-RAMP NEVER REACHED LONGITUDINAL CONTROL; A DRIVER ADJUSTMENT
   MID-RAMP DESTROYED THE CARRIED OFFSET
------------------------------------------------------------------------
* fix(SLA): `get_v_target_from_control` NOW RETURNS THE RAMP TARGET. `v_sla`
  goes into the speed governor's min() next to the cluster set speed, and it
  was returning `effective_speed_limit_target` — the CURRENT zone's value. So
  approaching a FASTER zone, SLA itself pinned the car to the old limit for the
  whole approach: the ramp did its job and walked the cluster up exactly as
  designed, and this one line threw the result away, because
  min(rising cluster, old zone target) is the old zone target. Nothing moved
  until the boundary re-seeded the zone target and the entire rise arrived as
  one step. Precisely the report: "I see the set speed going up, but the long
  control doesn't react... then it jumps fully."
  The DOWN ramp never showed it because there the cluster is the more
  restrictive of the two, so min() picked the ramp's value by accident — the
  descent behaviour is bit-identical, and a test pins that.
* fix(SLA): A CRUISE PRESS DURING A RAMP IS A DELTA, NOT AN ABSOLUTE.
  `_set_ratio_from_cluster` reads the whole cluster value as "what the driver
  wants relative to the current limit". True when the cluster is parked on
  limit*(1+ratio); FALSE for the entire duration of a ramp, because the ramp is
  what put the cluster there. Worked example, exactly the reported symptom:
  +30% carried into a 50 mph zone (target 65), descending toward a 30 zone,
  cluster walked down to 40. One tap of `+` and v3.4.5 stored
  (41 - 50)/50 = -18%. The carried offset is destroyed, and the 30 zone is then
  entered at 24.6 mph instead of 39 — "it forgets where SLA was set before so
  when we get to the new zone it's all buggy."
  While the ramp has the cluster DISPLACED, the press now moves the ratio by
  what the driver added ON TOP of the ramp's value. Parked on the zone target
  (no ramp) the old absolute derivation is still correct and still runs — which
  is also what keeps the +/-50% RATIO_LIMIT rail enforced.
* fix(SLA): a press no longer ABORTS the descent. v3.4.5 treated a button event
  like a boundary crossing: re-seed to `current_target` and clear `_latch` /
  `_d_min` / `_confirm_n` / `_engage_grace`. Both halves are wrong mid-ramp —
  re-seeding throws the set speed back UP to the zone target the driver was
  already descending away from, and clearing the state restarts the descent
  from scratch on every tap. The ramp now ADOPTS the driver's value, shifts the
  monotone-descent latch by the same delta so it cannot claw the adjustment
  back, and keeps the rest of its state.
* fix(cruise_ext): the post-press ramp hold is 100 -> 60 frames. It has to end
  at roughly the same time as SLA's own 0.5 s intent window; at 1 s, SLA's ramp
  was live again for half a second while this side still refused to follow it,
  so the cluster JUMPED when the hold finally expired.

4. SCC: ONE FEATURE INSTEAD OF A VETO BETWEEN TWO
------------------------------------------------------------------------
* feat(SCC): NEW `sunnypilot/.../long_v2/scc_fusion.py`. v3.3.8 made SCC-M's
  cap conditional on SCC-V being independently ACTIVE. The protection that
  bought is real and is PRESERVED EXACTLY — a map point on a straight road
  still cannot brake the car. But the veto did not ask "does the model see a
  corner?", it asked "has the model's own corner controller crossed its
  activation threshold?", which the map can rarely clear when it matters: a
  corner pulling 1.5 m/s^2 is a real corner and nowhere near activating vision,
  and the map reasons to 400 m while the model's plan reaches ~240 m at 30 m/s
  — so the veto was hardest exactly where the map's early, gentle reduction was
  most useful. Result: missed slowdowns.
  Corroboration is now CONTINUOUS and scales the map's AUTHORITY instead of
  switching it: vision active -> map passes through unchanged; vision sees a
  corner -> map takes a proportional share of the reduction it asked for,
  bounded by MAP_SOLO_MAX_CUT (~15 mph) so a partially-corroborated map error
  has a bounded cost; vision sees a straight road -> vetoed outright.
* fix(SCC-V): THE SELECTION MASK WAS ASKING THE MODEL'S OPINION OF ITS OWN
  PLAN. A plan point counted as a corner only when `orientationRate.z *
  velocity.x` exceeded the comfort limit — but `velocity.x` is what the model
  INTENDS to be doing there, and the model plans to slow for corners. So a real
  corner the model had already planned around read as "under the limit, nothing
  to do", while in `acc` mode the car never follows that planned velocity, so
  nothing slowed it. The corner SPEED never had this problem
  (v_i*sqrt(a/(rate*v_i)) is algebraically sqrt(a/curvature), independent of
  the planned velocity) — only the mask. A point now binds when its comfortable
  corner speed is below the speed we are ACTUALLY carrying,
  max(v_ego, v_cruise). The cruise term is load-bearing: with v_ego alone the
  mask empties the moment the car has slowed TO the corner speed, releasing the
  cap and oscillating inside the corner.
* tune(SCC): `a_lat_target` 2.4 -> 2.1 m/s^2 and the SCC-V horizon 7 -> 8 s.
  2.4 is brisk for a corner taken by a machine rather than by a driver who
  chose the line; at 30 m/s, 2.1 first constrains a ~430 m radius, a genuine
  sweeper rather than lane-keeping wander. The `+ A_DECEL_APPROACH * t` term
  already de-weights distant points, so the wider horizon costs no authority
  and buys earlier corroboration.

5. DEAD CODE SWEEP
------------------------------------------------------------------------
Everything here was verified unreferenced by an AST scan across the repo
before deletion, not by eye.

* chore: DELETED `sunnypilot/selfdrive/controls/lib/smart_cruise_control/` —
  the legacy v1 SCC package (6 files, 841 lines). `long_v2/` superseded it in
  v3.2.6e when the planner stopped importing it; it has had zero importers
  since and was shipping to the device every update. (The UI file
  `selfdrive/ui/sunnypilot/onroad/smart_cruise_control.py` is a DIFFERENT file
  and is alive.)
* chore: DELETED `long_v2/jerk_filter.py` — its only consumer was
  `following_v2.py`, deleted in v3.2.6e.
* chore: DELETED `long_v2/tests/test_physics.py`. It defined `k*sqrt(fric*g)`
  corner formulas LOCALLY and asserted on those, so it tested nothing in the
  codebase — and it had been failing 17 cases since v3.2.6e replaced the real
  formulas. A test that keeps its own copy of the maths cannot fail when the
  real maths changes, only when the copy drifts.
* chore: DELETED seven dead `LongV2Tuning` fields (`k_sccv`, `k_sccm`,
  `thw_default`, `d_standstill`, `jerk_limit_normal`, `jerk_limit_safety`,
  `decel_comfort`, `accel_comfort`, `speed_limit_offsets`), `fric.comfort_scale`,
  `tuning.reset_tuning_cache` and `elements.LeadSpeedElement`. The tuning
  fields were kept "so existing param JSON still parses", but `get_tuning()`
  already drops unknown keys — they bought nothing and read as live knobs.
* fix(ui): `_BadgeState` had TWO `__init__` definitions. The second wins (as
  always in Python) and is the CORRECT one — the first never set `_from`, which
  `tick()` reads on the frame after any `set_target()`. Deleting the first is a
  runtime no-op; resolving the duplication the other way would have shipped an
  AttributeError into the onroad UI on the first badge colour change.
* chore: ruff is now CLEAN across `selfdrive/ sunnypilot/ system/ common/`
  (was 13 errors: the banned `pytest.main`, an unused import, a duplicate
  `__init__`, an unnecessary `open(..., "r")` mode, and nine implicit
  multi-line string concatenations this repo's own config bans). A zero
  baseline is the only one where a new warning means anything.

TESTS
------------------------------------------------------------------------
* NEW `test_knot_filter.py` (16) and `test_lat_handback.py` (16); SCC, SLA and
  cruise_ext suites extended. Import-light throughout.
* Every load-bearing guard MUTATION-TESTED: publishing the zone target again,
  absolute ratio re-derivation mid-ramp, masking on the model's planned
  velocity, binary corroboration / unbounded solo cut, damping the raw action
  (becoming an EMA), a fixed release time constant, and the 1 s cruise_ext ramp
  hold — each reintroduction makes the suite fail, and each removal makes it
  pass again.
* fix(lat): while here — `_model_lookahead_curv` was measuring its horizon from
  `liveDelay.lateralDelay`, but with the lagd toggle on modeld_v2 places the
  ACTION at the CACHED lagd value. The lookahead is defined as one model step
  past the action horizon, so with an inflated lagd value it could land BEHIND
  the action and hand the v3.3.6 spline a reversed exit slope (bounded by the
  Fritsch-Carlson clamp, but wrong). It now reads the same `get_lat_delay`
  answer ControlsExt already computes, falling back to the live estimate.
* THE KNOWN-FAILURE LIST IS NOW EMPTY. v3.4.8 shipped with 20 red: 17 in
  `test_physics.py` (deleted above — it tested formulas the codebase no longer
  contains) and 3 in `test_triage_recorder.py::TestWebserverHelpers`, which
  turned out to be nothing but a missing `aiohttp` in the bare test container
  and pass as soon as it is installed. Off-device the import-light suite is
  348 green, 0 failed. If something fails now, it is real.
* `FUNNYPILOT_VERSION` -> 3.4.9; nav_webserver `EXPECTED_VERSION` -> "3.4.9",
  two new `_FEEL_FILES` rows and eight new `_CODE_MARKERS` rows.

FunnyPilot v3.4.8 (2026-07-27)
========================
Three defects in the v3.4.5 predictive set-speed ramp, all reported from one
drive into a HIGHER speed limit: the taper started too late and did not finish,
the set speed flickered a mph the wrong way mid-taper, and after entering the
new zone the car refused to accelerate for ~10 s -- the pedal did not help and
only cycling long control/SLA cleared it.

* fix(SLA): THE GAS GATE NO LONGER STRANDS THE CAR. `_update_gas_gate` tested
  pure set-speed geometry (`v_cruise_target < effective_speed_limit_target`)
  and never asked how fast the car was actually going. Its whole justification
  is "do not add throttle to FIGHT the ramp", and there is no fight when v_ego
  is already at or below the ramp target -- but the gate fired anyway and
  clamped `accel_clip[1]` to the coast accel, which is exactly a car that will
  not accelerate. Entering a zone below target (which the truncated up-ramp
  below made routine) with any lower zone inside v3.4.7's ~490 m envelope
  reproduces it. Three narrowings, all fail-safe:
    - v_ego must exceed the target by GATE_V_MARGIN (0.5 m/s);
    - compare against the CLAMPED target -- `v_cruise_target` goes through
      `_clamp_set_speed` and `effective_speed_limit_target` did not, so a
      target above V_CRUISE_MAX_KPH or below the min set speed made the
      comparison true FOREVER, a latched gate with no exit;
    - GATE_MAX_FRAMES (30 s) watchdog. The longest legitimate hold is one
      descent (~15.6 s); a car coasting on a highway is not an acceptable way
      to discover a latch.

* fix(SLA): THE UP-RAMP COULD NOT FINISH. It walked the set speed up over a
  FIXED 90 m while the output is bounded by a RATE (`RATE_MAX * DT_MDL`).
  Different units, so the window truncates whenever dv > RATE_MAX * (d/v_ego):
  at 55 mph, 90 m is 3.66 s and 3.66 * 1.2 = 9.8 mph of a 15 mph rise. The
  window is now a TRAVEL TIME sized from the rise itself (`t = dv / RATE_NOM`,
  bounded by RAMP_UP_T_MAX = 8 s), so it holds its meaning at any speed.
  RAMP_UP_T_MAX deliberately still truncates very large rises: finishing is not
  worth sitting 15 mph over the posted limit 300 m before the sign, and the
  boundary re-seed picks up the remainder.

* fix(SLA): ENGAGEMENT NOW HAS HYSTERESIS. CONFIRM_N guarded ENTRY but nothing
  guarded CONTINUATION -- one dropped mapd frame reset `_confirm_n` to 1 and
  the ramp fell through to `target = current_target`, walking the set speed the
  WRONG way for several frames. liveMapDataSP is 1 Hz, the route match blinks,
  and `d` reaches 0 before the current limit flips, so this happens routinely.
  A confirmed zone is now carried through a dropout by DEAD RECKONING
  (d closes at v_ego, which is what the car is really doing), bounded to 1 s by
  ENGAGE_GRACE_FRAMES.

* All three guards MUTATION-TESTED (bug reintroduced -> suite fails ->
  restored). Import-light speed-limit + car + controls suites: 201 green.

* `FUNNYPILOT_VERSION` -> 3.4.8 (v3.4.7 changed the ramp geometry but never
  bumped the file, so the device would have reported 3.4.6). nav_webserver
  `EXPECTED_VERSION` -> "3.4.8" plus four new `_CODE_MARKERS` rows.

FunnyPilot v3.4.6 (2026-07-26)
========================
Fixes the memory leak introduced in v3.4.5: the device reported low memory
shortly after going onroad, climbed steadily, and eventually died. One change
is responsible, and it is the `git gc` that v3.4.5's new startup storage
cleanup ran.

* fix(manager): REMOVED the `_git_gc()` step from
  `system/manager/storage_cleanup.py`. USER REPORT: "when I enter on road
  mode, I see low memory and the number creeps up until the device crashes."
  WHAT IT COST, measured rather than estimated. `git gc --prune=now` on the
  funnypilot repo (423 MB of packs, 4 cores) peaks at **1.69 GB RSS**:
  `git gc` shells out to `git repack`, which runs one `pack-objects` per core
  with an unbounded delta window. On a 4 GB device already running the onroad
  stack, with no swap, that is an OOM. Bounding it (`pack.threads=1`,
  `pack.windowMemory=16m`, `pack.deltaCacheSize=16m`) still peaked at 629 MB,
  so tuning was not a fix — removal is.
  WHY IT FIRED EVERY SINGLE DRIVE rather than occasionally: the gc sat behind
  the `low` free-space gate, whose thresholds (6 GB / 12%) are deliberately
  ABOVE `deleter.py`'s 5 GB / 10% floor. But deleter holds free space AT its
  floor by design, so free space hovers just under 6 GB forever and `low` is
  effectively ALWAYS TRUE on any device that has recorded real mileage. The
  gate that read as "only when space is actually low" was permanently open.
  WHY THE TIMEOUT DIDN'T SAVE IT: `subprocess.run(timeout=180)` kills only
  the direct child. `git gc`'s `repack`/`pack-objects` grandchildren survive
  it and keep allocating, so `GIT_GC_TIMEOUT_S` bounded nothing at all. A
  time bound is not a memory bound.
  WHY IT RACED THE DRIVE: `cleanup_async()` starts at `manager_init()`, i.e.
  seconds before the whole onroad stack comes up — the daemon thread and the
  car pull away together.
  TWO MORE REASONS IT CANNOT COME BACK, either one sufficient on its own:
  upstream openpilot DELIBERATELY disables on-device gc
  (`system/updated/updated.py:setup_git_options` sets `gc.auto=0` and
  `gc.autoDetach=false`) — v3.4.5 hand-rolled the thing upstream had switched
  off; and rewriting `.git` makes `updated.py:init_overlay` see
  `find .git -newer .overlay_init` come back non-empty, so it tears down and
  rebuilds the entire overlay on the next boot. The gc cost more disk than it
  reclaimed, i.e. it made the "storage full" symptom it was written for
  WORSE.
  Reclaiming `.git` is offroad maintenance, not a boot task. A device whose
  `.git` has genuinely run away wants a fresh clone.
* fix(manager): the rest of the cleanup is UNCHANGED and still runs — the
  unconditional removal of `/data/safe_staging/old_openpilot` (which is both
  the biggest reclaim and the thing that blocks every future update), plus
  `/data/core` and `/tmp/comma_download_cache` when space is low. Those are
  rmtrees; they are bounded in memory. The module is now stdlib-only in the
  strong sense: it spawns no processes at all.
* test: NEW `TestNoSubprocesses` in `system/manager/tests/
  test_storage_cleanup.py` — asserts on the AST (not on a substring, so a
  commented-out call cannot satisfy it) that the module imports no
  `subprocess`/`multiprocessing`/`asyncio`, makes no process-spawning call,
  never mentions git outside its docstring, and that `_git_gc`,
  `GIT_GC_MIN_BYTES`, `GIT_GC_TIMEOUT_S` and `BASEDIR` are gone. Plus
  `test_low_gate_is_not_rare`, which pins the "`low` is always true"
  consequence next to the threshold assertion that causes it, so the next
  person to hang work off that gate reads both at once.
  MUTATION TESTED: re-adding `subprocess.run(["git", ..., "gc"])` to
  `cleanup()` fails 3 of the new tests and passes none of them; restoring
  returns 31/31 green.
* NOT VERIFIED ON ROAD: this removes the leak's source rather than treating
  the symptom, so the check is simply that memory is flat across a drive. If
  it still climbs, the next suspect is NOT this module — nothing else added
  in v3.4.5 allocates per-frame — and the `storage`/`Disk usage` row in
  Verify plus the code-marker rows should be captured before retuning
  anything.

FunnyPilot v3.4.5 (2026-07-26)
========================
SLA stops reacting at the sign and starts planning for it. The headline feature
is a predictive set-speed ramp, but the load-bearing change is a clock-domain
fix without which that ramp — and the v3.4.0 one before it — was dead code on a
moving car.

* fix(speed limit): ROOT CAUSE of "it's too late". `speed_limit_resolver.py`
  computed map-data age as `time.monotonic() - unixTimestampMillis * 1e-3`,
  subtracting a Unix epoch (~1.8e9) from a since-boot counter (~1e4). On this
  device the result was about -1.785e9 s, so `distance_to_next_limit` came out
  near 3.9e10 m at highway speed. Every consumer of that number — SLA's
  pre-zone gas gate AND the v3.4.0 predictive ramp — was therefore inert while
  looking perfectly healthy, and what the driver actually felt at the boundary
  was nothing but the slew cap unwinding at ~9 mph/s.
  The staleness gate was equally broken: `age > LIMIT_MAX_MAP_DATA_AGE` could
  never fire for a real fix, so it was an accidental has-ever-had-a-GPS-fix
  test wearing a freshness test's clothes.
  Age now comes from `sm.recv_time['liveMapDataSP']`, which SubMaster stamps
  with the CONSUMER's `time.monotonic()` regardless of what language published
  the message. `logMonoTime` would NOT have been safe: it is `time.monotonic()`
  for Python publishers and `CLOCK_BOOTTIME` for C++ ones. Freshness is gated
  on `sm.valid` (literally `llk.gpsOK`) plus `MAP_MSG_MAX_AGE = 2.0`, wide
  enough to survive one dropped message on a 1 Hz service.
* feat(speed limit): predictive set-speed ramp. USER REPORT, verbatim: "it
  waits until we ENTER the new speed limit, where it quickly spams control
  commands to adjust speed one by one ... we need to move the logic to start
  adjusting prior to reaching the new speed limit ... I think 10-15 seconds is
  a safe range to target ... it needs to feel very natural."
  `_update_cruise_ramp()` walks the set speed down a distance-parameterised
  constant-decel envelope `v_set(d) = sqrt(v_next^2 + 2*a*d_eff)`. Distance,
  not time: no `v_ego` division, so it cannot blow up at standstill, and it is
  self-correcting — every frame re-solves from the CURRENT distance, so a
  late-appearing zone, a route change or a slow-down all converge without any
  timer state to get stuck.
  The rate is chosen so the whole adjustment spans the user's window:
  `RATE_NOM = 0.45` m/s per second (~1 mph/s), capped at `RATE_MAX = 1.2`,
  engagement bounded by `RAMP_T_MAX = 15.0` s and `RAMP_D_MAX = 250.0` m, and
  the target is reached `RAMP_ARRIVE_EARLY_T = 1.0` s before the boundary.
  `RATE_MAX` is not a taste value: `CRUISE_MIN_ACCEL = -1.2` in `long_mpc.py`
  is a structural ceiling, so any faster set-speed slew would be display-only
  theatre the car never follows.
* fix(car): `cruise_ext.update_speed_limit_assist_v_cruise_non_pcm` had TWO
  unchained `if` blocks both assigning `self.v_cruise_kph` — the documented
  "idempotent boundary snap" and the ramp follow — with the ramp last. The snap
  was overwritten before it ever reached the car: dead code that read like the
  authoritative path. SLA now owns the value end to end, and an AST guard keeps
  the second writer from returning.
* fix(car): the ramp writes on the DISPLAY GRID (1 kph metric,
  `IMPERIAL_INCREMENT = round(CV.MPH_TO_KPH, 1)` imperial), the same lattice
  the cruise buttons produce. A raw continuous target would park the set speed
  somewhere the driver could not have set it, and their next `+` press would
  snap to the nearest grid point, silently eating part of the change. It is
  also what makes the ramp read on the cluster as ordinary 1-mph taps.
* fix(speed limit): ratio re-derivation is now gated on a recent button event
  (`BUTTON_INTENT_FRAMES`). Mid-approach the cluster sits BETWEEN zones, so
  re-reading the offset from a cluster change the ramp itself caused would let
  SLA silently collapse a carried +20% into whatever value the ramp happened to
  be passing through. This replaces the v3.4.0 `_cluster_change_is_ours()`
  one-frame history, which could not distinguish the two on a multi-frame ramp.
* fix(speed limit): `sla_shm.py` gained a timestamp field and `STALE_S = 0.5`.
  cruise_ext writes this channel's target into `v_cruise_kph` at 100 Hz, so
  without a freshness check a wedged plannerd left the last value in the file
  forever and the car kept obeying a process that no longer exists — including
  reverting the driver's own SET+ press a fraction of a second after they made
  it. THE FAILURE MODE OF THIS CHANNEL MUST BE "NO REQUEST", NEVER A STUCK ONE.
* feat(manager): NEW `system/manager/storage_cleanup.py`, called from
  `manager_init()` on a background thread (never synchronously — a blocking
  call would add `du` + `git gc` to every boot). USER REPORT: "when I flashed
  this version I got a 'storage full' message briefly."
  It is an ALLOW-LIST, not a walk-and-decide: absolute paths only, no globs,
  and a test asserts `/data`, `/data/openpilot`, `/data/params`, `/data/media`
  and `/` can never appear in it. `cleanup()` is total — no input makes it
  raise, because it runs before the car can start. Thresholds sit ABOVE
  `deleter.py`'s 5 GB / 10% floor so this pass engages before drive logs are
  eaten, not after.
* feat(web): the FunnyPilot version is now shown in the web UI title bar,
  served from a new `/api/version` endpoint reading `FUNNYPILOT_VERSION`. A new
  `storage` row in Verify reports filesystem use% (read-only) so the next
  occurrence of the storage message produces evidence instead of a memory.
* test: 291 green off-device (was 200). Three new suites — the resolver clock
  guard (11), the shm freshness contract (19), and cruise_ext's ramp half (10)
  — plus a rewritten storage-cleanup suite. FIVE independent mutation tests
  were run and confirmed to fail-then-restore: the epoch bug (5 failures), the
  shm age check (2), the second `v_cruise_kph` writer (1), the missing display
  quantization (3), and ungated ratio re-derivation (2).
  Two pre-existing tests were found to be VACUOUS while fixing them up — both
  ran so few frames that the slew cap dominated and their two branches produced
  identical values. They now run to settling.
* NOT VERIFIED ON ROAD / FALSIFIABLE: the ramp now depends on OSM's
  `speedLimitAheadDistance` being a real metre value. If the next drive still
  shows the adjustment beginning only at the boundary, check the `storage` and
  code-marker rows in Verify and the `liveMapDataSP` freshness FIRST — do not
  retune the ramp constants until the distance is confirmed sane.

FunnyPilot v3.4.4 (2026-07-26)
========================
The longitudinal status dot now answers the question it was actually built to
answer: are my brake lights on? It stops calling reduced throttle "braking".

* fix(ui): the status dot went red on ANY commanded deceleration, so lifting to
  a lower throttle lit it up exactly like a brake application. USER REPORT,
  verbatim: "it's showing red when we're commanding any decceleration ... so
  the dot goes red even if we're still using the throttle but at a lower
  amount. I want it as a true reflection of the vehicles controls status."
  The premise was wrong, not the threshold: `aReqValue < 0` is a request to
  slow down, and on this platform the ESC decides whether to satisfy it by
  cutting throttle or by pressing the brakes. The sign of the command cannot
  answer the question, at any tuning.
  New semantics: red = the car's brake lamps are lit, green = throttle
  commanded, gray = gas gating / off throttle / long control inactive.
* feat(car): NEW `sunnypilot/selfdrive/car/brake_light_shm.py` + a two-line
  read in `opendbc/sunnypilot/car/hyundai/carstate_ext.py`. Red now comes from
  `TCS13.BrakeLight`, a bit the ESC broadcasts about ITS OWN actuator — lamps
  lit or not, whoever asked (driver pedal, ACC, AEB). TCS13 is already decoded
  on that exact line for `aBasis`, and the classic-CAN parser is built with an
  empty signal list, so this costs nothing on the CAN side.
  NOT A RETURN TO v3.3.9. That version compared the commanded accel against
  `get_coast_accel(pitch)` and was rejected for making the dot a function of
  IMU-derived road grade. A reported lamp state is the opposite of inferred
  physics: no threshold, no coast line, no pitch, no measured acceleration.
  `_BRAKE_FIRM` — the last fixed coast-ish threshold in the file — is deleted,
  and a test asserts it cannot come back.
* feat(car): the bit crosses processes over `/dev/shm/fp_brake`, same reason
  and same pattern as `sla_shm.py` / `lat_interp`: no `cereal/*.capnp` change,
  no device rebuild. (`CarState.brakeLights` exists upstream only as
  `brakeLightsDEPRECATED`.) The publisher writes on every transition and
  otherwise at 5 Hz, so a steady state costs five atomic writes a second.
  LOAD-BEARING: the reader returns `None` (UNKNOWN), never `False`, when the
  file is missing or stale. False means "the car says the brakes are off";
  None means "nobody told us anything", and classify() falls back to the old
  commanded-decel rule instead of confidently claiming brakes-off on a car or
  build that has no publisher.
* test: 21 new cases, 200 green off-device. `test_long_status_dot.py` rewritten
  around the new contract, with `TestReducedThrottle` as the direct regression
  guard for this bug report. NEW `test_brake_light_shm.py` (13 cases: round
  trip, garbage, staleness, publisher rate limiting, never-raises). NEW
  `test_brake_light_signal.py` — asserts `BrakeLight` really is a TCS13 signal
  in `hyundai_kia_generic.dbc` and that carstate_ext spells it that way. That
  one is a boot-path guard, not a nicety: `cp.vl["TCS13"]["BrakeLight"]` raises
  KeyError on an unknown name, inside card, at 100 Hz — a typo there is a car
  that does not drive, the same failure class as v3.4.0/v3.4.1.
  All three guards were MUTATION-TESTED (bug reintroduced -> suites fail ->
  restored), per the standing rule.
* KNOWN ASYMMETRY, deliberate and documented in the module: the car publishes a
  brake lamp but no equally unambiguous "throttle applied" bit, so green still
  keys off the commanded accel. A steady cruise that holds speed with real
  throttle but a ~zero accel command reads gray, not green. Fixing that
  honestly needs an engine-torque signal (`EMS16.TQI` / `TCS13.TQI_SCC`) whose
  "any throttle" boundary is not obvious — it is NOT to be papered over with
  another threshold.
* ON-ROAD VERIFICATION REQUIRED, and it is directly falsifiable: this assumes
  the K5's ESC raises `TCS13.BrakeLight` for ACC-commanded braking, not only
  for the driver's pedal. If the dot never goes red while openpilot brakes,
  that assumption is dead and the next signal to try is `SCC12.StopReq` /
  `TCS13.DriverOverride`.
* chore: `FUNNYPILOT_VERSION` -> 3.4.4, `EXPECTED_VERSION` -> "3.4.4", two new
  Verify code markers (32 total).

FunnyPilot v3.4.3 (2026-07-25)
========================
Recovery + hardening release. Gets the car off the splash screen, fixes the
deploy path that had been silently dropping every flash, and generalizes the
v3.4.2 boot guard so the next occurrence of that bug class is caught anywhere
in the tree. The two v3.4.0 features (SLA predictive set-speed ramp, long
status dot) are carried forward unchanged and are finally running on-device.

* fix(deploy): ROOT CAUSE of "my flashes silently didn't apply". It was never
  credentials — the device fetches over HTTPS from a public repo. 1194 files
  inside `/data/openpilot/.git` were owned by root (left behind by `sudo git`
  / root-run flashes), INCLUDING `.git/HEAD` itself. As user `comma`,
  `git fetch` failed with `Permission denied` on `.git/logs/refs/...` and
  `git checkout` could not rewrite HEAD, so the `&&` chain died before
  `reset --hard` and before the reboot. The device kept running old code while
  the deploy command looked like it had worked. Fixed on-device with
  `sudo chown -R comma:comma /data/openpilot/.git`; CLAUDE.md now documents
  the symptom, the fix, and the rule to never run `git` under `sudo` there.
  Related trap now documented: `git ... | tail` returns TAIL's exit status,
  so piped git checks report success unconditionally — use `${PIPESTATUS[0]}`.
* feat(test): NEW `sunnypilot/tests/test_capnp_annotations.py` — repo-wide AST
  guard against capnp types in `|` unions. v3.4.2's guard only covered
  cruise_ext.py; the next instance will be in a different file. This one walks
  all ~1900 .py files and reports only annotations Python actually EVALUATES:

    RAISES  def f(x: car.CarState | None)         parameter annotation
    RAISES  def f() -> car.CarState | None        return annotation
    RAISES  class C: CP: car.CarParams | None     class-BODY annotated assign
    SAFE    self.CP: car.CarParams | None = None  inside a method body
    SAFE    x: car.CarState | None = None         local in a function body

  The class-body form is a NEW finding — the previously recorded rule only
  covered parameter annotations, but a class-body annotation is evaluated too
  and is equally fatal. All five behaviors were verified empirically rather
  than assumed.
  It is an AST walk and not a grep precisely because of the SAFE rows: a text
  matcher would demand a bogus "fix" to `ui_state.py`, which is correct as
  written. Subscripted forms are also SAFE and are not flagged —
  `list[custom.X] | None` builds a `types.GenericAlias`, which DOES implement
  `__or__` (verified), so `sunnypilot/models/fetcher.py:126` is correct and
  was deliberately left alone after the first draft of the detector flagged it.
  Both this guard and v3.4.2's were mutation-tested: the bug was reintroduced
  into cruise_ext.py and both suites were confirmed to FAIL, then restored.
* docs: corrected the capnp misattribution in `sla_shm.py`,
  `longitudinal_planner.py`, `cruise_ext.py` and `long_status_dot.py`. Those
  comments still claimed the `cereal/custom.capnp` change caused the v3.4.0
  boot failure. It did not — the annotation did. The /dev/shm approach STAYS
  (avoiding device rebuilds is right on its own merits), but the stated reason
  is now accurate so the next debugging session isn't sent down a dead end.
* chore: `.claude/` removed from `.gitignore` (Claude Code config now tracked).
  ALL VPN/proxy remote-access documentation stripped from CLAUDE.md (no longer
  used) — SSH Access is now just the home-network line and Deploying to Device
  uses a plain `ssh` invocation, plus a mandatory post-deploy verification
  block (version + hash + port 8888).
* test: 163 pre-existing cases still green, +16 new (13 detector-semantics
  cases, 2 self-checks that the repo scan isn't vacuous, 1 repo sweep).
  Off-device runs need Python 3.11 — the repo uses PEP 585 generics, and a
  3.8 interpreter fails collection with `'type' object is not subscriptable`.

FunnyPilot v3.4.2 (2026-07-25)
========================
HOTFIX. This is the ACTUAL fix for the unbootable car. v3.4.0 AND v3.4.1
both hang at the comma splash screen — do not flash either.

* fix(boot): ONE type annotation was the whole problem:

      def update_speed_limit_assist_v_cruise_non_pcm(self, CS: car.CarState | None = None)
      TypeError: unsupported operand type(s) for |: '_StructModule' and 'NoneType'

  `car.CarState` is a capnp _StructModule, not a Python type, so the `|`
  union operator raises while the CLASS BODY is evaluated at import time.
  That import is on manager's startup path (manager -> process_config ->
  mapd_manager -> osm_map_data -> base_map_data -> selfdrive.car.cruise ->
  cruise_ext), so manager died before starting a single process and the
  device never left the splash screen. Fixed by dropping the annotation.
* CORRECTION: the v3.4.1 notes blamed the `cereal/custom.capnp` change for
  the failed boot. That was WRONG — the device log shows no build error at
  all, just this TypeError. v3.4.1 removed the capnp fields but kept the
  annotation, so it would have failed identically. The capnp revert and the
  /dev/shm channel from 3.4.1 are KEPT anyway (they work, they avoid a
  device rebuild, and they were an explicit request), but they were not the
  cure and are not described as such anymore.
* test: NEW `sunnypilot/selfdrive/car/tests/test_cruise_ext_imports.py`.
  Nothing in the suite imported cruise_ext, which is why two consecutive
  releases tested green and still bricked the car. Two guards: an actual
  import of the module (compiled-only deps stubbed), and a source scan for
  capnp types in `|` unions. Both were verified to FAIL when the bad
  annotation is reintroduced, not just to pass now.
* Swept the rest of the tree for the same pattern. The three other hits
  (`ui_state.py`) are attribute annotations inside method bodies, which
  Python never evaluates — confirmed harmless by direct test. Only function
  PARAMETER annotations are evaluated at definition time.
* chore: FUNNYPILOT_VERSION -> 3.4.2, EXPECTED_VERSION -> "3.4.2". Suite
  163 green.

FunnyPilot v3.4.1 (2026-07-25)
========================
HOTFIX for v3.4.0, which would not boot — the car sat at the comma splash
screen and restarting did not help. DO NOT FLASH 3.4.0.

* fix(boot): v3.4.0 added four fields to `cereal/custom.capnp`. A .capnp
  change makes SCons regenerate and recompile the schema on the device at
  next boot; that build is what hung the boot. Nothing else in either
  feature needs compiling — the whole path is Python (plannerd, card) plus
  Python/raylib (UI). All four fields are REMOVED and `cereal/` is now
  byte-identical to 3.3.8, so the existing prebuilt binaries stay valid and
  there is nothing to rebuild.
* The two values that genuinely had to cross a process boundary (SLA's
  set-speed ramp target, plannerd -> card; and SLA's gas-gate flag,
  plannerd -> UI) now travel over `/dev/shm/fp_sla` via the new
  `sla_shm.py` — the same mechanism controlsd already uses for
  `/dev/shm/lat_interp`, so it is this fork's established pattern rather
  than a new one. Writes are atomic (temp file + os.replace) so a reader
  can't see a torn line; every read is best-effort and falls back to
  "no request" on a missing/garbage file, so a telemetry failure can never
  affect control.
* Behavior of both v3.4.0 features is otherwise UNCHANGED: SLA still walks
  the real cruise set speed before a zone change (the thing that works
  under DEC), and the status dot still reads `carOutput.actuatorsOutput.accel`,
  the literal aReqValue sent to the car.
* LESSON (recorded in CLAUDE.md): on this device, touching any .capnp is a
  COMPILED change and risks an unbootable car. Prefer /dev/shm for
  fork-internal cross-process values.
* chore: FUNNYPILOT_VERSION -> 3.4.1, EXPECTED_VERSION -> "3.4.1". Suite
  161 green; shm round-trip verified including garbage/missing-file paths.

FunnyPilot v3.4.0 (2026-07-25)
========================
Branched fresh from funnypilot-3.3.8. v3.3.9 is ABANDONED — both of its
features were built on wrong premises (details below) and are not carried
forward. Do not flash 3.3.9.

* fix(long): SLA predictive decel/accel now works under DEC, by moving the
  ACTUAL CRUISE SET SPEED — i.e. doing exactly what the driver would do
  tapping +/- on the wheel — instead of shaping an internal planner target.
  WHY v3.3.9 FAILED: it added a `SlaSpeedRamp` that pre-ramped the
  `v_cruise` value fed to the MPC. In DEC's blended mode that value is only
  a weakly-weighted position cap (0.1) competing with the model's own
  acceleration plan (5.0), so shaping it changed almost nothing — the same
  root cause the ramp was meant to fix. The set speed, by contrast, is the
  one quantity EVERY mode honors identically: in acc mode it is the cruise
  obstacle, in blended mode it is the position cap, and it is what the
  cluster displays. Moving it cannot be ignored by whichever mode DEC
  happens to pick.
  `SpeedLimitAssist._update_cruise_ramp` now publishes `vCruiseTarget` —
  the set speed the cluster should read right now — and
  `cruise_ext.update_speed_limit_assist_v_cruise_non_pcm` follows it in
  whole display units. Down into a slower zone: constant-decel envelope
  (RAMP_DECEL = 0.8 m/s^2, sqrt/distance form, standstill-safe), converging
  on the new target at the boundary. Up into a faster zone: linear over the
  last RAMP_UP_DIST = 90 m, so the set speed blends up into the new zone as
  requested. Slew-capped at RAMP_MAX_RATE = 4 m/s^2-equivalent so the
  displayed number can never jump.
  LOAD-BEARING GUARD: SLA re-derives the driver's carried offset ratio from
  any cluster set-speed change. Mid-approach the cluster sits BETWEEN zones,
  so re-deriving there would silently wipe the offset (a +20% carried
  preference would collapse). `_cluster_change_is_ours()` recognizes the
  ramp's own commands and suppresses re-derivation for them only; genuine
  button presses still set the ratio exactly as before, and the boundary
  snap stays idempotent. Directly unit-tested (3 cases).
  cruise_ext also holds the ramp for ~1 s after any cruise button event, so
  a driver adjustment reaches SLA instead of being overwritten next frame.
  KNOWN LIMITATION (unchanged from the gas gate): needs map-source ahead
  data; car-state/dash-recognized limits have no lookahead distance and
  still change at the boundary.
* feat(ui): longitudinal status dot, bottom-left, always on. gray = neither
  gas nor brakes commanded (incl. gas gating and long control off), red =
  deceleration commanded at any rate, green = acceleration commanded at any
  extent.
  WHY v3.3.9 FAILED: it compared the commanded accel against a
  PITCH-DERIVED coast estimate (`get_coast_accel`) to decide what counted
  as braking, making the dot a function of IMU-inferred road grade — the
  "acceleration sensing" this readout was explicitly supposed to avoid.
  Now it reads `carOutput.actuatorsOutput.accel`, which for this car is the
  literal value the carcontroller packs into SCC12's `aReqValue` (see
  `new_actuators.accel = self.tuning.actual_accel` in hyundai
  carcontroller.py) — the last software layer between openpilot and the
  car, no estimate and no measurement anywhere in the decision. Gas gating
  reports gray via the control code's OWN published flags (SLA's pre-zone
  gate, now published as `assist.gasGating`, plus the existing SCC-V/SCC-M
  gate flags) rather than by trying to recognize a coast-shaped number;
  firm commanded braking still overrides a gate flag so a real brake
  application is never masked. Classification factored into a pure
  `classify()` and unit-tested (9 cases), including a regression guard that
  its inputs contain no pitch/measured-accel term.
* cereal: `SpeedLimit.Resolver` gains `nextSpeedLimitFinal @9` /
  `distToNextSpeedLimit @10`; `SpeedLimit.Assist` gains `gasGating @7` /
  `vCruiseTarget @8`.
* chore: FUNNYPILOT_VERSION -> 3.4.0; nav_webserver EXPECTED_VERSION ->
  "3.4.0" + markers for the ramp and the dot. Full import-light suite 161
  green (pre-existing, unrelated test_physics.py failures excluded as
  documented since v3.3.3).

FunnyPilot v3.3.8 (2026-07-21, continued)
========================
A real fix for the turn-in/railroad-track torque oscillation, plus a
dev-UI readout swap. Per explicit user direction, this is an ACTED-ON
mechanism (not a fully proven one) — instrumentation alone had converged
on a plausible, code-verified explanation, and the user asked for a
fix now rather than more logging.

* fix(lat): NEW `BumpDamper` (`selfdrive/controls/lib/bump_damper.py`),
  wired into `latcontrol_torque.py`. Mechanism, verified line-by-line
  against the actual code (not just asserted): (1) `get_friction()`'s
  slope inside its threshold band is `friction * latAccelFactor /
  FRICTION_THRESHOLD` — for this K5 (fitted friction 0.1165) that's
  ~1.6 with the fork-LOCKED latAccelFactor of 2.750, i.e. TWICE the
  PID's own KP of 0.8, live exactly in the small-error turn-in regime;
  (2) that same locked 2.750 is ~14% above the K5's fitted 2.405
  (`opendbc/car/torque_data/params.toml`), so feedforward under-delivers
  ~12.5% of the torque actually needed, pushing more work onto the
  high-gain relay in (1); (3) the PID setpoint is the model's desire
  from ~lat_delay (~0.5s) ago (the delay buffer), and the jerk
  lookahead replays the same buffer ~0.3s later — so a bump-induced
  measurement disturbance (bump-steer / momentary grip or
  self-aligning-torque change from weight transfer) corrupts BOTH the
  measurement (during the event) and the delayed setpoint (0.3-0.5s
  later), landing squarely in the high-gain relay above for long enough
  to ring a couple cycles — matching the observed lag between the BUMP
  pitch-rate peak and felt oscillation onset.
* On a detected pitch-rate spike (>5 deg/s, reusing the existing
  car-frame IMU signal — no new subscription), BumpDamper blends the
  measurement TOWARD the setpoint (shrinking |error| — NOT holding
  it, which under a ramping setpoint would manufacture GROWING error
  and thus MORE torque, exactly backwards), damps the jerk-lookahead's
  contribution to the friction relay by the same factor, and freezes
  the PID integrator — for 0.8s past the last supra-threshold frame
  plus a 0.7s linear recovery. Floor 0.40 (matching the fork's other
  established floors: lane-change 0.45, override 0.6). This can only
  ever soften the correction TOWARD the plan's own feedforward — it
  cannot add torque or lose the corner, and composes safely downstream
  of nothing (it's upstream of the EPS governor and panda, both
  untouched backstops).
* Verified: unit tests for timing/bounds (11 cases), plus a closed-loop
  plant simulation of a delayed-PID + friction-relay loop under a step
  measurement disturbance — post-bump torque ringing dropped ~74% and
  peak error during the event ~60% in that simplified plant. This is a
  logic sanity check, not proof the real vehicle behaves this way.
* FALSIFIABLE with the EXISTING dev-UI BUMP readout, no new
  instrumentation: if the oscillation still occurs while BUMP shows
  >5 deg/s (damper provably engaged that frame), this mechanism is
  dead and the next suspect is the model's own plan, not the
  controller's reaction to it.
* feat(ui): L.S. (lead speed, redundant with REL SPEED/REL DIST) removed
  from the bottom dev-UI bar for torque cars; new LIM element shows
  whether the EPS governor's driver-torque clamp is ACTIVELY biting the
  request right now (fraction of the last second's control frames with
  `driver_limited=True`) — distinct from EPS's authority ceiling, which
  can sit below 100% without the request ever actually reaching it.
* fix(long): `SCCMapV2`'s speed cap no longer binds on its own — it now
  requires `SCCVisionV2` to also be actively constraining
  (`gate_map_target` in `speed_governor.py`). Previously the governor
  took the min() of vision and map independently, so a map false
  positive (mistagged/rounded curve speed, stale OSM data) could brake
  the car even when the model's own view of the road ahead saw nothing.
  Vision keeps full independent authority to slow down; map can only
  ever narrow vision's cap once vision agrees a reduction is warranted,
  never introduce one on its own. 3 new unit tests.
* feat(ui): SCC-V/SCC-M badges now 8% larger, and show map-vs-vision
  arbitration: when BOTH are actively constraining, the one the
  governor is following tints toward vivid blue with a white ring, the
  other fades toward slate — tint strength scaled by how far apart
  their targets are (agreeing controllers keep the plain
  disabled/armed/gas-gate/braking colors unchanged).
* IMPORTANT CORRECTION to the v3.3.8 EPS-governor work above: user
  reports the "torque, back off, torque, back off" oscillation still
  occurs with the EPS authority readout pinned at 100% — meaning the
  hardware driver-torque clamp was NOT engaged during those specific
  events. That doesn't undo the clamp fix (it's still correct
  whenever the clamp *does* engage), but it means the clamp is NOT the
  explanation for at least one recurring case: crossing railroad
  tracks mid-corner. User's alternate hypothesis, physically
  plausible and NOT yet verified: the bump unloads the front/steering
  axle (weight transfer), which could reduce grip (outward slip) or
  reduce the self-aligning torque needed for a given angle (so the
  same commanded torque now yields a bigger angle than the controller
  expects), and the car's suspension does not settle instantly —
  spring/damper rebound continues to disturb the front axle for a
  beat afterward, which could explain a repeating, not single, event.
* feat(diag): new UNVERIFIED bump/weight-transfer instrumentation,
  deliberately NOT paired with any control-loop change yet (same
  discipline as every other hypothesis in this file: instrument,
  drive, correlate, THEN fix — guessing wrong here has repeatedly cost
  a full version in this project's history). Reads car-frame angular
  rate (approx. pitch — nose dip/rebound) from the IMU pose controlsd
  already computes every frame for carControl (no new subscription).
  Dev-UI bottom bar gains "BUMP" (1 s max-hold, deliberately
  uncolored — no claimed thresholds yet). Triage lat records gain
  "pit" (per-second peak). NEXT STEP: correlate "pit" spikes against
  the felt oscillation and against "eps"/"dtx" in the SAME second — a
  pit spike with eps pinned at 100% would support the physics theory;
  no pit spike would falsify it too and send us looking elsewhere.
* chore: nav_webserver gains code markers for gate_map_target,
  SuspensionBumpElement, and BumpDamper; bump_damper.py added to the
  feel-file hashes. Full import-light suite 141 green.

FunnyPilot v3.3.8 (2026-07-19)
========================
Two fixes: the turn-in "grab torque, immediately loosen, hard steer again"
oscillation, and E2E experimental longitudinal restored to a first-class,
DEC-compatible mode. (3.3.7 skipped; numbered per request.)

* fix(lat): EPS torque governor (`eps_limit.py`, wired last in
  latcontrol_torque). ROOT CAUSE of the grab/loosen cycle: the
  carcontroller AND panda clamp commanded torque by the driver-torque
  limit — for the K5, allowed = 384 + (50 − |sensor|)·2, slewed +3/−7
  per 10 ms — and the torsion-bar sensor can read wheel-INERTIA
  reaction during hard self-steer bites, not just the driver's hands.
  HYPOTHESIS, not verified: the v3.2.8-era "sensor hits 150+" figure
  was inferred, never measured, and that era's fix didn't cure the
  oscillation. What is certain: the clamp exists, engages from a
  sensor reading of just 50, and is invisible to the tuning layer. If
  it engages: bite → sensor spike → hardware sheds at −7/frame →
  sensor relaxes → controller (blind to the clamp) re-bites at
  +3/frame — re-excited by every model knot on turn-in. New triage
  fields make one drive decisive: "dtx" (max |raw sensor|/s), "eps"
  (min governor authority/s), "tqd" (max requested-vs-applied
  divergence/s) — see CLAUDE.md for the interpretation matrix. The governor mirrors the exact hardware
  bounds + slew inside the controller so the request is always
  realizable, collapses with the bound instantly, but RECOVERS at
  0.35/s — under half the hardware rate — which is the damping that
  breaks the limit cycle. Also fixes a real windup bug: the clamp
  engages at sensor 50–150 where steeringPressed (threshold 150) never
  fires, so the PID integrator wound up against an invisible limit and
  slammed on release; the governor's clamp state now freezes the
  integrator and feeds the saturation alert (sustained authority loss
  in a corner stays driver-visible). Only ever reduces torque; panda
  enforcement untouched. Triage lat_interp.jsonl gains "eps" (per-sec
  min authority) — 1.0 means the clamp never engaged that second.
* feat(long): MPC 'blended' mode restored (deleted in the v3.2.6e
  single-authority rewrite). Pure E2E experimental could not accelerate
  because e2e was reduced to min(action.desiredAcceleration, ACC-MPC) —
  for non-mlsim bundles (generation < 11) that action accel isn't
  meaningful, freezing acceleration. Now, exactly like upstream: for
  non-mlsim bundles the MPC itself tracks the model's x/v/a trajectory
  in blended mode; for mlsim bundles the min-blend applies only when
  the mode is blended. Dynamic Experimental Control arbitrates
  acc/blended when enabled — and ALL fork longitudinal features keep
  working in both DEC modes by construction: SCC-V/M, SLA and the
  hidden governor shape v_cruise upstream of the MPC, which binds as
  the cruise obstacle in acc mode and as the blended position cap in
  blended mode; the SLA gas gate, AccelJerkShaper, LeadGrace, and the
  fork 70% accel clip apply to the output in every mode. ACC-mode MPC
  behavior is byte-identical to 3.3.6.
* feat(ui): SCC-V/SCC-M badges 8% larger, and they now show the
  map-vs-vision arbitration: when BOTH controllers are producing a
  speed cap, the one the governor actually followed tints toward vivid
  blue and wears a white ring, the out-voted one fades toward slate —
  with the tint strength scaled by how much the two disagree (agreeing
  controllers keep the normal disabled/armed/gas-gate/braking colors).
* feat(ui): dev-UI INTERP readout retired (it was a static "5" — the
  spline interpolation is knot-exact by construction) and replaced by
  the two values that discriminate the turn-in oscillation: "EPS %"
  (torque authority the hardware driver-torque clamp is passing, 1 s
  min-hold; 100 green / <100 orange / <60 red) and "TBAR" (raw
  torsion-bar reading, 1 s max-hold; green <50 = below clamp
  threshold, orange 50-149 = clamp band that steeringPressed can't
  see, red >=150). Glance rule: note what these show during an event
  vs. the all-green normal. controlsd's /dev/shm/lat_interp heartbeat
  now carries "n,authority".
* chore: FUNNYPILOT_VERSION -> 3.3.8; nav_webserver EXPECTED_VERSION ->
  "3.3.8", new markers (EpsTorqueGovernor, MPC blended restore),
  eps_limit.py added to the feel-file hashes. test_eps_limit.py NEW
  (10 cases incl. request-always-realizable vs the real opendbc clamp
  function). Full import-light suite 127 green. NOTE: blended-mode MPC
  behavior needs the aarch64 acados solver — validate on-device.

FunnyPilot v3.3.6 (2026-07-18)
========================
Lateral: the delay-window smoothing feel returns — without the onset
distortion that got the v3.2.11/3.2.12 EMA reverted in v3.3.2.

* feat(lat): LatSmoother gains a SPLINE method (default). The validated
  delta/5 TIMING contract is untouched — every 20 Hz model knot value is
  still reached exactly on the validated schedule, and a flat desire can
  never creep — but the 100 Hz path between knots is now a C1
  shape-preserving monotone cubic instead of a piecewise-linear ramp.
  The linear ramp's steering rate jumped at every model frame (a 20 Hz
  slope staircase — the residual harshness the old EMA used to mask);
  the spline carries the realized output slope across each knot, so the
  rate is continuous through sustained maneuvers. On a constant-rate
  maneuver the spline is bit-identical to the validated delta/5
  schedule; it only differs where the linear scheme kinked. Simulated
  corner profile: peak jerk (second difference of the command) drops
  ~70% with identical knot timing, identical peak curvature.
* feat(lat): the exit slope of each 50 ms segment is aimed using a
  one-model-step lookahead read from the model's own published plan
  (`get_curvature_from_plan` at lat_delay + 2*DT_MDL) — the "free
  compute inside the lagd delay window" idea, reintroduced as a pure
  read: it shapes only the sub-period path, never filters a knot, so it
  cannot shift maneuver onset (the v3.3.2 post-mortem stays honored; the
  do-not-reintroduce note in lat_smooth.py now spells out the
  distinction). When the plan says the desire flattens (apex), the wheel
  eases off and settles instead of arriving at full rate — the well-liked
  v3.2.2 SETTLE feel, emerging from the clamped slope. Lookahead
  unavailable/insane -> plain secant (the validated ramp shape).
* safety: both end slopes are clamped to the Fritsch-Carlson monotone
  box, so the output provably stays inside the model's [prev, cur]
  desire bracket (hard-clamped as well), lands on cur exactly when the
  linear ramp would, and restarts monotone from zero slope on direction
  reversals. A single late knot carries the aimed slope (no mid-corner
  ease-in restart); a genuine model stall decays it to zero. LINEAR
  remains one constructor argument away for an A/B flash.
* chore: `FUNNYPILOT_VERSION` -> 3.3.6, nav_webserver `EXPECTED_VERSION`
  -> "3.3.6", controlsd marker grep v3.2.10 -> v3.3.6, new SPLINE code
  marker; test_lat_smooth.py extended to 19 cases (constant-ramp
  bit-compat, flat-never-creeps, knot-exact timing, C1 carry, apex
  settle, adversarial lookaheads, saturated-frame regression).

FunnyPilot v3.3.5 (2026-07-16)
========================
Fix: soundd no longer crashes (and no longer triggers the "Communication
Issue between Processes" takeover alert) when the audio output stream
goes inactive at runtime.

* fix(soundd): stock openpilot ends every soundd loop iteration with
  `assert stream.active` — if the PortAudio output stream dies at runtime
  (audio device hiccup; the `@retry` on `get_stream` only protects the
  initial open), the AssertionError kills the whole process, and
  selfdrived's process watchdog raises the driver-facing takeover alert.
  Seen in the wild on-device 2026-07-14 (`soundd.py line 180 ...
  AssertionError`). soundd_thread now runs the poll loop `while
  stream.active` and, when the stream goes inactive, closes it and
  recreates it via the existing `get_stream` (which re-terminates and
  re-initializes portaudio, with `@retry(attempts=10, delay=3)`).
* A genuinely dead audio device still surfaces as before, by
  construction: if the stream can't be reopened, `get_stream`'s retry
  raises and the process dies (-> process alert); if streams open but
  keep dying young, a guard counts consecutive streams that lived < 10 s
  and raises after 5, so the process can't silently spin with no audible
  alerts.
* chore: `FUNNYPILOT_VERSION` -> 3.3.5, nav_webserver `EXPECTED_VERSION`
  -> "3.3.5".

FunnyPilot v3.3.4 (2026-07-16)
========================
Fix: the hidden cruise governor no longer lifts while following a lead —
a detected lead can no longer pull the car above the governed max speed.

* fix(hidden cruise governor): v3.3.3st gated the governor off whenever
  the MPC's active constraint was a lead (`lead0`/`lead1`), so the moment
  a lead was detected the speed ceiling snapped from 93% of the set speed
  back to 100% — observed on the road as up to ~6 mph over the governed
  max while sticking with a lead, and as "sticky" overspeed during lead
  handoffs (the one-frame-lagged `following` flag plus LeadGrace's
  v_ego-floored cap held the higher speed through flicker). The
  `not following` term is removed: `HIDDEN_CRUISE_OFFSET` now applies to
  the cruise ceiling unconditionally (still skipped before `vCruise`
  initializes and during a forced decel, where the 20% under-`v_ego`
  clamp already owns the target). Safety is unaffected by construction —
  the governed `v_cruise` is only the MPC's cruise-obstacle ceiling;
  braking for a slower lead is owned by the MPC's lead constraint, which
  sits below the ceiling whenever it matters. The governor itself
  (`HIDDEN_CRUISE_OFFSET = 0.93`, hidden from all driver-facing UI/state)
  is intentionally unchanged.
* chore: `FUNNYPILOT_VERSION` -> 3.3.4, nav_webserver `EXPECTED_VERSION`
  -> "3.3.4". `_CODE_MARKERS` unchanged (the `HIDDEN_CRUISE_OFFSET`
  marker still matches).

FunnyPilot v3.3.3st (2026-07-14)
========================
Stable snapshot of 3.3.3 plus three hands-off quality-of-life features:
a hidden cruise-only speed governor, unattended model/map refresh while
parked on WiFi, and a live-learned steer-delay readout on the dev UI.

* feat(hidden cruise governor): restores the pre-3.2.6e "hidden speed
  offset" concept (previously 0.9x, removed in the single-authority
  longitudinal rewrite for predictability). `HIDDEN_CRUISE_OFFSET = 0.93`
  in `longitudinal_planner.py` shaves 7% off the cruise-only target speed
  before it ever reaches the MPC/controls layer — nothing in the UI or
  car state shows it. It only applies while simply tracking the set
  speed: gated off the instant a lead is being followed (lead0/lead1) or
  a forced decel is in progress, so lead braking and safety stops are
  completely unaffected.
* feat(auto-updater): new `sunnypilot/auto_updater/manager.py` daemon
  (`only_offroad`-gated, mirrors `models_manager`/`mapd_manager`). Tracks
  continuous WiFi (`deviceState.networkType == wifi`) while parked; once
  held for 15 minutes it re-triggers the same actions the Settings
  "CHECK"/"Database Update" buttons do — nudges `ModelManager_DownloadIndex`
  to the currently active bundle (hash-verified per-file, so it's a
  no-op unless the bundle's remote content changed) and, if a map region
  is configured (`OsmLocal`), sets `OsmDbUpdatesCheck` to refresh the OSM
  data. Re-arms every 15 minutes so a long parked/charging session keeps
  refreshing both.
* feat(dev UI lagd readout): bottom developer-UI bar gains a "LAGD" tile
  (`LagdElement` in `selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py`)
  showing `liveDelay.lateralDelay` live (green when the live-learner's
  estimate is vetted/`estimated`, red if `invalid`, white while still
  `unestimated`) — the same value the Models page's "Live Learning Steer
  Delay" toggle feeds from, now visible on the road without opening
  Settings.

FunnyPilot v3.3.3 (2026-07-13)
========================
SLA gets its original arrow activation back, learns to gas-gate BEFORE a
lower speed limit zone, and the logging/web UI is decluttered. Lateral is
untouched (3.3.2 feel carries over byte-identical).

* feat(SLA activation, the original controls + UI): when long control is
  engaged and a speed limit is known — on engage or on entering a new zone
  while SLA is off — the sign pulses and shows an up/down arrow for 6
  seconds: up if your set speed is below the limit, down if above.
  Pressing the cruise button IN THE ARROW'S DIRECTION during the window
  activates SLA; the press is swallowed and your CURRENT SET SPEED IS
  ADOPTED UNCHANGED — no jump, no jerk. The %-offset is derived from where
  you already are (confirm at 50 set in a 45 zone = active at +11%, still
  doing 50). If the set speed already equals the limit, SLA activates by
  itself at 0%. The window simply times out otherwise and re-offers at the
  next zone. Everything AFTER activation is the new stack, unchanged: the
  cluster set speed is the target, manual adjustments re-derive the
  carried %-offset (60 in a 50 -> +20% -> 36 in a 30 zone), zone snaps are
  idempotent, deactivation only on disengage/mode off.
* feat(SLA pre-zone gas gating): approaching a LOWER zone while active,
  once inside the coast envelope ((v^2 - v_target^2) / (2 * 0.35) plus a
  1.5 s early-arrival buffer, target including your %-offset), the planner
  clamps max accel to the measured coast accel — the same mechanism the
  model's allow_throttle uses. No throttle, NO brakes: the braking floor
  is untouched (lead braking unaffected), and the cruise target does not
  drop until the boundary, so nothing can command brakes for the new zone
  early. Enter the zone near target; any residual overspeed is shed by
  the MPC as normal LIGHT braking after the boundary.
* fix(resolver): the upstream "adapt to the upcoming limit early" switch
  (marked FIXME/not-working upstream) is removed — it flipped the resolved
  limit ~80 m early, which would have made the SLA set-speed snap fire
  before the zone, i.e. braking before the sign. The resolver now changes
  exactly at the boundary and instead exposes the upcoming limit +
  distance (speedLimitAhead) continuously for the gas gate.
* chore(logging cleanup): web UI Verify consolidated from ~31 rows to 7 —
  version / branch / working-tree-clean / ONE "shipped code markers" check
  (16 load-bearing greps, fails naming whatever is missing) / updater
  target+staged / active model bundle / triage log sizes. The inline
  log-tail rows are gone (that's what the Logs viewer is for).
  lat_interp.jsonl no longer writes a record every parked second: idle
  time collapses to one {"idle": N} heartbeat per minute, and the first
  driving record carries the skipped count. radar_tracks.jsonl likewise
  collapses zero-track seconds to a 30 s heartbeat — "n stuck at 0 while
  driving" is still fully visible, without 86k identical lines a day.
* tests: SLA suite rewritten for the arrow flow (30 cases: directional
  confirm both ways adopts the set speed, wrong-direction ignored, stale
  press expiry, window timeout + re-prompt, adopted ratio carries to the
  next zone, ratio carryover incl. both user examples, gas gate envelope
  on/off/ratio-aware/clears-at-boundary); triage idle-collapse cases
  added (112 total import-light tests green).

FunnyPilot v3.3.2 (2026-07-11)
========================
REVERTS the v3.2.12 adaptive EMA smoothing after on-road falsification —
steering was starting EARLIER than the validated feel ("a 2.5 in the space
before we'd usually see the 5"), and raising the delay knob only smeared it
further into sloppiness.

* post-mortem (recorded so it is never retried): the v3.2.12 idea was
  "EMA the knots with tau, sample the plan tau earlier, lag is pre-paid, so
  the effective total is unchanged". The flaw: an EMA does not DELAY a
  maneuver, it REDISTRIBUTES it. For a turn onset, the filtered command
  begins moving immediately at the (earlier) sample point and creeps
  through partial values across the window — so the wheel is visibly
  turning where the pre-3.2.12 command was still flat before its decisive
  step. "Total-preserving" holds for steady-state phase, not for onset
  shape, and onset shape is what hands feel. No delay-knob setting fixes
  it: more delay = an earlier sample point = more smear.
* the delay window IS still used for interpolation — the way that was
  always validated: the models-page delay makes every 20 Hz knot a
  delay-compensated preview, and controlsd's LatSmoother spreads each knot
  delta across the control frames (delta/5). That stays, untouched.
* reverted: modeld_v2 adaptive tau + horizon shift (bundle 'lat' override
  restored to plain upstream additive semantics; network delay input
  unchanged throughout); stock modeld same; smooth_seconds_for_delay
  removed from lat_smooth.py (a DO-NOT-REINTRODUCE note remains).
  controlsd's setpoint alignment (lat_delay = lateralDelay) is kept — it
  was a genuine pre-existing fix and is exactly correct with the EMA gone.
* lateral timing is now byte-equivalent to 3.2.10/3.3.0e-as-validated for
  stock-override bundles. RESTORE YOUR MODELS-PAGE DELAY to your preferred
  per-model value (e.g. 0.35) — the compensation you added against the
  early-steer artifact is no longer needed.
* radar-tracks work (3.3.0e/3.3.1) carried unchanged.
* chore: FUNNYPILOT_VERSION -> 3.3.2; EXPECTED_VERSION -> 3.3.2;
  code_smoothsec replaced by code_smoothrev (verifies the revert is the
  code actually running).

FunnyPilot v3.3.1 (2026-07-11)
========================
Radar-tracks ENABLE is now evidenced and verified (lateral confirmed good
on 3.3.0e, carried unchanged). The 3.3.0e log showed the radar's OUTPUT but
nothing about the enable handshake itself — and the upstream enable had a
real honesty bug: it fetched the write response with timeout=0, never
checked it, and never read the config back, so "successfully enabled" (and
radarUnavailable=False) could be reported when the radar had NACKed the
write. That produces exactly a log full of lead distances with zero real
track points.

* fix(opendbc enable_radar_tracks): success is now claimed ONLY when the
  post-write read-back shows the tracks bit set. Write ack is checked with
  a real timeout; a NACKed/silent write returns False so radarUnavailable
  correctly stays True (clean stock fallback instead of a dead parser).
* feat: the full handshake is appended to radar_enable.jsonl (web UI ->
  Logs; Verify -> triage_radaren shows the last boot inline): per attempt
  {session answered?, current config hex, write ack?, post-write verify
  hex, enabled}, plus the radar's DEVICE FINGERPRINT read in-session
  (UDS DIDs: application software id 0xF181, part number 0xF187, HKG
  version blob 0xF100) — what's needed to match this DL3 radar against
  known-good tracks configs and pick an alternate payload if 0142 is
  rejected. Exceptions land in the log too. Logging is best-effort and
  can never break the enable itself.
* feat: radard writes a radar_identity record at startup into
  radar_tracks.jsonl: carFingerprint, radarUnavailable (the enable's
  claimed outcome), and ALL ECU firmware versions from openpilot's
  ignition-time FW query (incl. the fwdRadar ECU) — the car-side half of
  the fingerprint.
* chore: FUNNYPILOT_VERSION -> 3.3.1; EXPECTED_VERSION -> 3.3.1; new
  code_radaren + triage_radaren Verify rows.
* tests: NEW opendbc test_enable_radar_tracks.py (5 cases with a scripted
  fake UDS query: verified success; NACKed write MUST fail — the exact
  upstream bug; silent radar; already-enabled short-circuit; unwritable
  log dir never breaks the enable) + 2 identity-record cases. Full suite
  green.
* READING THE LOG: radar_enable.jsonl "session":false => radar never
  answered 0x7D0 (wiring/bus); "write_ack":false or verify unchanged =>
  firmware rejected config 0142 (send the ident block + we try the
  alternate payload next); "enabled":true + radar_tracks.jsonl n>0 =>
  done, tracks are real.

FunnyPilot v3.3.0e (2026-07-10)
========================
EXPERIMENTAL — enables RADAR TRACKS on the 2021+ Kia K5 (DL3) and logs them
to the web UI, groundwork for radar-grounded longitudinal tuning.

* HOW IT WORKS: the sunnypilot base already auto-enables Mando radar
  tracks (opendbc/sunnypilot _initialize_radar_tracks: a UDS config write
  to the radar at 0x7D0 on every ignition, panda-safety allowlisted, retry
  x2) — but only for platforms carrying HyundaiFlags.MANDO_RADAR, and
  KIA_K5_2021 didn't have it. Added the flag: the platform gains the
  hyundai_kia_mando_front_radar DBC, the enable runs at car init, and on
  success the radar broadcasts its raw track table (RADAR_TRACK_500-51f,
  32 slots, 50 Hz) which openpilot's radar interface parses into
  liveTracks. If the radar declines the write, radarUnavailable stays True
  and EVERYTHING behaves exactly as today (vision + SCC lead emulation) —
  graceful, no fault path. The radar's own SCC function is not affected by
  the tracks-output bit (long-established community config).
* STOCK ACC IS ENOUGH FOR LOGGING — openpilot long NOT required: the
  enable runs in card's fingerprint/init path before long-control mode
  matters, the 0x7D0 TX is allowlisted in panda safety unconditionally,
  radard runs onroad regardless of long mode, and liveTracks publishes
  either way. Drive normally on stock ACC and the log fills.
* feat: NEW RadarTracksMonitor (triage_recorder.py) wired into radard —
  1 Hz records in radar_tracks.jsonl (web UI -> Logs): n/nmin/nmax track
  count (n pinned at 0 in traffic = enable didn't take), 3 closest points
  [dRel, yRel, vRel], radarState leadOne/leadTwo [dRel, vLead, aLeadK],
  radar CAN error count. Duck-typed + fully try/excepted: telemetry can
  never take radard down (garbage-input test included).
* Verify gains: code_radartrk (flag present) and triage_radar (last
  radar records inline).
* chore: FUNNYPILOT_VERSION -> 3.3.0e; EXPECTED_VERSION -> 3.3.0e.
* tests: 4 new RadarTracksMonitor cases; opendbc hyundai platform suite
  green with the flag (13 passed, 383 subtests).
* NOTE: first drive, open Verify -> triage_radar or Logs ->
  radar_tracks.jsonl. Healthy = n in the 5-25 range in traffic with
  plausible closest-point distances. n = 0 everywhere means the DL3
  radar firmware rejected the config write — copy the log anyway and
  we'll try the alternate enable payload next.

FunnyPilot v3.2.12 (2026-07-10)
========================
SUPERSEDES 3.2.11 — DO NOT FLASH 3.2.11. Two corrections from user feedback
and a deeper code read:

  (1) The models-page delay knob (0.29-0.39 per model, user-tuned, feeds the
      model network itself via lateral_control_params) is the USER'S — the
      smoothing must adapt to it, never require changing it.
  (2) 3.2.11 edited the WRONG DAEMON for this device: custom Model Manager
      bundles run through sunnypilot/modeld_v2 (NativeProcess
      modeld_tinygrad), which has its own per-bundle smoothing override
      ('lat', default 0). 3.2.11's stock-modeld constant would have done
      nothing there while controlsd's +0.2 misaligned the PID setpoint
      buffer by 0.2 s. Also corrected: latDelay 0.478->0.497 DRIFTS in the
      logs, i.e. it is the live learner's measured value — the K5's true
      command-to-response lag is ~0.5 s.

* feat: delay-funded adaptive smoothing (lat_smooth.smooth_seconds_for_delay):
      tau = clip(0.4 * lateral_delay_in_use, 0, 0.3)
  and the action horizon is sampled EARLIER by tau, so the EMA's lag is paid
  from inside the delay window: effective total ALWAYS equals the configured
  delay. Set the knob higher -> more smoothing time; lower -> less — the
  window is finally "filled with something useful" at any setting. At the
  user's current ~0.5 s in-use delay: tau = 0.2 s.
* modeld_v2 (the daemon this device runs): tau = bundle 'lat' override if
  set, else the adaptive budget; the generation >= 10 EMA gate is respected
  (older bundles get tau = 0 AND no horizon shift, so nothing steers early);
  the NETWORK still receives the user's full delay via
  lateral_control_params — per-model delay tuning is byte-identical.
* stock modeld: same total-preserving scheme (LAT_SMOOTH_SECONDS constant
  now only a zero fallback); controlsd aligns the PID setpoint buffer on
  lateralDelay directly (the old +constant also silently ignored per-bundle
  overrides when modeld_v2 was active — pre-existing misalignment fixed).
* SAFETY PASS (pre-drive review, all paths): budget function total on
  None/NaN/inf/negative/string -> 0.0 (smoothing off, tested); sample
  horizon floored at DT_MDL so curv_from_psis divides by t >= 0.1 with
  v clipped >= 1; np.interp inputs stay well inside T_IDXS; NO new array
  indexing anywhere; smooth_value guards tau <= 0; LatSmoother passes
  through before its first knot, holds on NaN, resets on lat-inactive;
  latcontrol delay_frames clip handles any delay >= 0; modeld_v2 test stub
  covered by getattr fallback; no new Params reads in hot loops.
* chore: FUNNYPILOT_VERSION -> 3.2.12; EXPECTED_VERSION -> 3.2.12;
  code_smoothsec now greps smooth_seconds_for_delay in modeld_v2 (the
  daemon actually running on this device).
* tests: 4 new budget cases (knob scaling, cap, degenerate-input safety,
  budget < delay always); full suite 55 green.

FunnyPilot v3.2.11 (2026-07-10)
========================
One feel change, cleanly A/B-able against 3.2.10: spend the preview window
on smoothing — the user's original "use the artificial delay to interpolate
in realtime" concept, implemented through the mechanism upstream already
plumbed for exactly this.

* WHAT WAS FOUND: the models-page software delay (LagdToggleDelay ~0.35 s,
  used when the live-learning toggle is off: delay = steerActuatorDelay +
  LagdToggleDelay ~= 0.15 + 0.35 = 0.50 s — matching the 0.478-0.497
  latDelay in the triage logs) RESERVES a preview window but nothing ever
  SPENT it: modeld's LAT_SMOOTH_SECONDS EMA — whose lag is pre-paid by
  adding LAT_SMOOTH_SECONDS to both modeld's action horizon and controlsd's
  lat_delay, so total reaction time is unchanged — arrived set to 0.0 via a
  sunnypilot base sync. The window sat idle as pure dead time.
* feat: LAT_SMOOTH_SECONDS 0.0 -> 0.2. The 20 Hz desired-curvature knots
  are now themselves smooth (EMA tau 0.2 s + the existing 2.5 m/s^3 jerk
  clamp), so the deltas that LatSmoother spreads per-frame get smaller and
  more consistent — many small wheel movements instead of a few bites —
  with zero added reaction time (paid from preview, not response).
* RECOMMENDED PAIRING: reduce the models-page software delay 0.35 -> ~0.15
  so TOTAL preview stays ~0.5 s (0.15 hardware + 0.15 software + 0.2
  smoothing); this re-allocates idle dead time into active smoothing.
  If steering feels EARLY (turning in before the curve), reduce it more.
* feat: triage ctx gains latDelayEst (liveDelay.lateralDelayEstimate) next
  to latDelay (the value in use). If est << used, we are steering
  systematically early — evidence for trimming the artificial delay
  further (the suspected contributor to "bite then loosen").
* chore: FUNNYPILOT_VERSION -> 3.2.11; EXPECTED_VERSION -> 3.2.11; new
  code_smoothsec self-check. Lateral interpolation (LatSmoother), torque
  features, override gate, triage recorder all unchanged from 3.2.10.

FunnyPilot v3.2.10 (2026-07-10)
========================
Restores the VALIDATED lateral interpolation feel. The 3.2.9e PlanRider
experiment is deleted after one drive: "feels like 3 updates a second,
two 45-degree bites instead of ten 9-degree ones".

* post-mortem (why PlanRider staircased): the plan-sampling formula
  (2*psi/(v*t) - psi_rate/v) is nearly t-INVARIANT inside a curve —
  advancing the sampling horizon between model frames barely moved the
  output, and each new plan then delivered the entire 50 ms of turn
  progression as ONE step. That is the stock 20 Hz staircase reborn, with
  its biggest steps exactly in sharp turns, grouped by the jerk clamp
  into a few large surges. The idealized-ramp unit tests passed because
  synthetic ramp plans are the one case where riding is smooth; real
  plans are state-anchored and quasi-steady in curves. Lesson recorded:
  smoothness must be guaranteed BY CONSTRUCTION (spread the knot delta),
  not hoped for from a formula's behavior between knots.
* feat: NEW selfdrive/controls/lib/lat_smooth.py — LatSmoother, the
  months-validated 3.1.0e delta/5 schedule in its minimal robust form:
  on each 20 Hz model action, prev <- last OUTPUT, cur <- new action;
  every 100 Hz frame emits prev + clip(elapsed/T_MODEL + 0.2, 0, 1) *
  (cur - prev). Bit-compatible with the validated feel at healthy 100 Hz
  (0.2/0.4/0.6/0.8/1.0 x delta), provably moves EVERY control frame,
  output always inside [prev, cur], continuous at ANY cadence (prev is
  the last output, so early/late knots can never step the command),
  holds cur if the model stalls, NaN-safe. No SETTLE, no lookahead, no
  health blend, no frame counters — 40 lines, nothing left to degrade.
* removed: lat_plan_rider.py + its tests. Dev-UI INTERP gauge and triage
  hmin/havg return to realized control-frames-per-model-frame (5 =
  healthy). Torque-side features and the 3.2.8 override gate untouched;
  triage recorder unchanged.
* chore: FUNNYPILOT_VERSION -> 3.2.10; EXPECTED_VERSION -> 3.2.10;
  code_planrider -> code_latsmooth; code_controlsd greps v3.2.10;
  _FEEL_FILES hashes lat_smooth.py.
* tests: test_lat_smooth.py (9 cases: exact validated schedule,
  moves-every-frame, bracket containment, early/late-knot continuity,
  model-stall hold, 50 Hz cadence independence, NaN hold, re-engage,
  health counting).

FunnyPilot v3.2.9e (2026-07-10)
========================
EXPERIMENTAL — deep reset of the lateral smoothing stack, replacing every
interpolation concept since v3.0.2e with one idea: RIDE THE PLAN.

Hardware findings that motivated keeping this in software (2021 K5 DL3,
Mando/Mobis MDPS): the LKAS torque interface runs at 100 Hz (LKAS11,
STEER_STEP=1) — the SAME rate as the comma's control loop, and the EPS's
internal motor loop is faster still, so there is NO update-rate mismatch.
The real hardware limits are: (1) torque slew caps of +3/-7 counts per
10 ms frame of a 384-count max (full authority takes ~1.3 s to ramp in —
an EPS fault-tolerance constraint, not tunable), (2) modest total assist
authority, (3) a measured ~0.48-0.50 s command-to-response lateral delay
(liveDelay, includes EPS + chassis). None of these are removable in code;
all of them are exactly what delay-aware control is for. Conclusion: the
smoothing problem is legitimate software territory, but the old stack was
solving the wrong formulation.

* feat: NEW selfdrive/controls/lib/lat_plan_rider.py — PlanRider. Every
  approach since 3.0.2e (frame counters, time-anchored knot interpolation,
  PHASE_LEAD, SETTLE ease-outs, lookahead weights, health blends)
  interpolated between 20 Hz POINT SAMPLES of the model's plan. But the
  model publishes its entire smooth plan every frame. PlanRider evaluates
  the plan itself at a continuously advancing horizon:
      t = lat_delay + DT_MDL + (time since the plan was captured)
  The plan segment from t to t+50 ms is by definition what the model wants
  the car doing until the next update — riding it gives per-frame-smooth
  curvature with ZERO added lag (we sample the plan's future, never filter
  its past). Plan handoffs are continuous by construction (successive
  plans are evaluated at the same wall-clock target instant); genuine
  model revisions are bounded by a single 2.5 m/s^3 lateral-jerk clamp —
  the ONLY shaping constant left in the lateral path.
* Properties the old stack needed machinery for, now free: cadence
  robustness (a late model frame is ridden further along the current plan
  — extrapolating the model's own intent — instead of stalling; > 0.2 s
  stale degrades to hold), no lane-change special case (nothing to force
  off), NaN/short plans fall back to the model's action value (= stock).
* removed: lat_interp.py (LINEAR/SETTLE) + its tests + the INTERP_METHOD
  switch + controlsd's _model_lookahead_curv. The /dev/shm/lat_interp
  dev-UI heartbeat and triage hmin/havg fields now carry PlanRider plan
  freshness (5 = fresh, 0 = stale/held) — same scale, same consumers.
  Torque-side features (lane-change scale, re-engage ramp, smooth stop,
  3.2.8 override gate) are untouched.
* note: the pasted triage window for this report happened to cover only
  parked idle (la=0, v=0, tqx=0 throughout) — the drive around the user's
  mark wasn't in the copied tail, so the 3.2.8 gate verdict is still
  open; the recorder keeps running unchanged on this branch.
* chore: FUNNYPILOT_VERSION -> 3.2.9e; EXPECTED_VERSION -> 3.2.9e;
  code_latinterp -> code_planrider check, code_controlsd greps v3.2.9e,
  _FEEL_FILES hashes lat_plan_rider.py.
* tests: test_lat_plan_rider.py (9 cases: exact tracking on constant-
  curvature plans, NO-STAIRCASE invariant on ramps, continuous handoffs,
  jerk-clamped model revisions, late-frame ride-through, stale hold +
  health decay, fallback, bad-plan rejection, reset seeding).

FunnyPilot v3.2.8 (2026-07-07)
========================
Fix for the "bite then loosen" lateral oscillation reported on the first
3.2.7 drive: strong back-and-forth alternation during large steering
adjustments — a hard initial bite, an immediate ~40% loosen, repeating at a
few Hz.

* fix: the v3.2.3st driver-override softening could limit-cycle against the
  controller's own output. It scaled TOTAL steering torque to 60% the
  instant CS.steeringPressed latched — but steeringPressed is just
  torsion-bar torque over a threshold (HKG: 150 counts, 5-frame debounce),
  and a hard steering bite can cross it with NO driver involved: the wheel
  rim's own inertia (or a lightly resting hand) resists the rapid
  acceleration and twists the bar. Full torque -> wheel accelerates ->
  "pressed" -> torque cut to 60% AND PID integrator frozen -> wheel
  decelerates -> bar relaxes -> "pressed" clears -> full torque bites
  again. NEW selfdrive/controls/lib/override_gate.py: the softening now
  engages only after a SUSTAINED press (0.4 s continuous) and releases only
  after a sustained let-go (0.3 s), so it structurally cannot alternate.
  Inertia blips (~0.1-0.25 s) never qualify; a genuine takeover engages
  ~0.4 s in and holds steady through threshold flicker. The driver always
  wins physically regardless — panda driver-torque limits and the EPS are
  untouched, and the takeover-comfort feature is preserved.
* note: this is the hypothesis the 3.2.7 instrumentation was built to test
  (spe/ovr fields). The instrumentation stays on in 3.2.8 — if oscillation
  persists, spe/ovr/sat/slb in lat_interp.jsonl will say what it actually
  is; if it stops, the log will show spe blips with ovr pinned at 1.0
  (gate rejecting them). The reported "5 -> 3" was illustrative, not
  measured; the gate is safe either way because it only ever REDUCES how
  often the softening can engage.
* chore: FUNNYPILOT_VERSION -> 3.2.8; EXPECTED_VERSION -> 3.2.8; new
  code_ovrgate self-check.
* tests: test_override_gate.py (8 cases: the limit-cycle pattern can never
  engage, sustained press engages/holds/releases correctly, per-frame
  toggling cannot cycle the state, reset).

FunnyPilot v3.2.7 (2026-07-05)
========================
Forensics build for the RETURNED "smoothing feels turned off after the car
sits parked" issue (came back after ~8 h parked despite the 3.2.5st
updater-revert guards). This version changes NO control behavior vs 3.2.6e —
it adds a black-box flight recorder so the next occurrence produces evidence
instead of a feeling. Copy the logs from the web UI when it happens; the
root-cause fix ships in the next version once the data says which
hypothesis is real:

  A. CODE SWAP — something still replaces the code while parked/at boot.
     nav_webserver logs a code-identity record at startup and every 10 min
     (branch, commit, dirty flag, FUNNYPILOT_VERSION, UpdaterTargetBranch,
     staged-update branch, .overlay_consistent, boot_id, uptime, and sha1
     hashes of the four feel-defining files: lat_interp.py, long_shaping.py,
     controlsd.py, latcontrol_torque.py). Pulses only write full records on
     CHANGE (pulse-change) — so if a swap happens at 3am while parked, the
     log pins down when, not just that.
  B. RUNTIME DEGRADATION — code fine, but lat_interp loses sub-frame
     headroom or falls into fallbacks. controlsd writes a 1 Hz record while
     onroad: interp health min/avg (5 healthy, <4 degrading — min is kept
     so a transient stall can't be averaged away), lat/long active
     fractions, v_ego, lane-change flag, ISO curvature-clip count, long
     aTarget vs commanded accel.
  C. TUNING DRIFT — same code, different feel: every 10 s the 1 Hz record
     embeds a live-tuning context (torque latAccelFactorFiltered, torque
     friction, angleOffsetDeg, stiffnessFactor, lateralDelay) — if the
     learners moved while parked, it shows here.

* feat: NEW selfdrive/controls/lib/triage_recorder.py — TriageRecorder
  (size-capped rotating JSONL, /data/funnypilot_triage, 4MB + .1 backup,
  all IO best-effort so telemetry can never break controls) +
  LatInterpMonitor (100 Hz samples -> 1 Hz aggregate records).
* feat: web UI "Logs" button — list, view (128K tail), and one-tap COPY of
  every triage log, plus a purple "Mark issue now" button that appends a
  timestamped marker (with optional note) to marks.jsonl so the subjective
  moment can be lined up with the recordings.
* feat: /api/logs, /api/logs/{name}?tail_kb=N, POST /api/logs/mark
  (filename whitelist, no path traversal); Verify gains code_triage +
  triage_boot/triage_lat info rows showing the latest records inline.
* chore: FUNNYPILOT_VERSION -> 3.2.7; EXPECTED_VERSION -> 3.2.7.
* tests: test_triage_recorder.py (10 cases: rotation, 1 Hz cadence,
  min-not-averaged aggregation, context cadence + exception containment,
  name whitelist, hash helper).
* feat (after first 3.2.7 drive): lateral-oscillation evidence for the new
  "bite then loosen" report. The 1 Hz record gains: sp (steeringPressed
  fraction), spe (steeringPressed RISING EDGES per second — a
  driver-override limit cycle is directly countable), ovr (minimum
  driver-override torque scale, 1.0 = off / 0.6 = fully softened = exactly
  the reported 5 -> 3 torque drop), sat (lat controller saturation
  fraction), slb (steer_limited_by_safety fraction), tqx (max |commanded
  torque|). PRIME SUSPECT under test: the v3.2.3st driver-override
  softening — a hard steering bite twists the torsion bar past HKG's
  STEER_THRESHOLD (150, 5-frame debounce) via wheel-rim inertia/resting
  hand, steeringPressed latches, torque is scaled to 0.6 AND the PID
  integrator freezes, the wheel decelerates, pressed clears, full torque
  bites again -> 2-4 Hz limit cycle. spe >= 2 with ovr 0.6 during an
  oscillation event confirms it; fix lands in 3.2.8. Still zero
  control-behavior changes in 3.2.7.

FunnyPilot v3.2.6e (2026-07-05)
========================
EXPERIMENTAL — longitudinal control rewritten around a single-authority
architecture. Design rules, in strict priority order, of what an autonomous
vehicle owes its passengers longitudinally:
  1. Safety is never comfort-limited. The MPC owns the safe-following problem
     (headway, braking envelope, danger-zone constraint, FCW); nothing
     downstream may weaken or delay its braking. Heuristics may shape its
     INPUTS (cruise speed, headway) but never clamp its output.
  2. Comfort is enforced in exactly one place: a single asymmetric jerk
     shaper. Throttle applies gently; braking slew scales with the demanded
     deceleration; FCW bypasses shaping entirely.
  3. Predictability: set speed means set speed; the command is a
     deterministic function of the plan.
  4. Robustness lives in the speed domain: lead flicker/departure handling
     can hold the car back but can never brake it.

* feat: NEW selfdrive/controls/lib/long_shaping.py — AccelJerkShaper (up-jerk
  1.4-2.5 m/s^3 by personality; down-jerk 4 m/s^3 for mild demands scaling
  continuously to 12 m/s^3 at -3.5 m/s^2, so hard braking is executed near-
  unshaped) and LeadGrace (on losing a lead we were actually following for
  >= 1 s: hold cruise at the lead's last speed 1.5 s, ramp out over 2 s; cap
  floored at v_ego so it can never command braking).
* fix(SAFETY): removed the FollowingControllerV2 accel/jerk overrides. Its
  0.5 m/s^3 "normal" jerk cap applied to the FINAL output could delay a
  3 m/s^2 braking demand by several seconds while its TTC tiers escalated,
  and its tier logic fought the MPC's own (correct) solution to the same
  problem. Following is now owned solely by the MPC.
* feat: follow distance is a constant TIME headway per personality
  (aggressive 1.25 s / standard 1.60 s / relaxed 2.05 s, +0.35 s cushion
  below city speeds) replacing the 3.2.5st speed-indexed tables that were
  most cautious where risk is lowest (3.75 s at city speed, 1.5 s at
  highway speed). COMFORT_BRAKE 2.0 -> 2.2 (earlier-than-stock brake
  initiation WITHOUT the closing-rate obstacle inflation hack, which
  double-counted braking distance and caused early/phantom braking).
  STOP_DISTANCE 11 -> 7.5 m (roomier than stock 6 m, no longer invites
  cut-ins). Relaxed personality now also gets a higher MPC jerk cost (2.0).
* fix(predictability): removed the hidden 0.9x cruise offset (car now
  actually drives the set speed), the 4 s personality-switch gas gate (the
  MPC's accel-change cost already smooths headway transitions), the lead-cap
  blend, and the asymmetric output filter (instant-down/0.35 s-up) — all
  replaced by the one shaper stage.
* feat: longcontrol.py — bumpless transfer: entering PID from
  stopping/starting seeds the integrator so the first frame continues from
  the last commanded accel instead of stepping; the starting state slews
  toward startAccel at 6 m/s^3 instead of stepping (kills the launch
  head-snap, still fast enough to release brake-hold).
* chore: deleted sunnypilot/.../long_v2/following_v2.py; SP planner keeps
  only the speed-domain governors (SCC-V/SCC-M/SLA/weather/road caps).
* test: NEW selfdrive/controls/lib/tests/test_long_shaping.py (22 cases:
  jerk asymmetry, strong-braking-barely-delayed safety invariant, FCW
  bypass, NaN containment, LeadGrace arm/hold/release/never-brake
  invariants); test_longcontrol.py gains bumpless-entry and starting-ramp
  cases. All import-light, run without the full openpilot env.
* feat: Speed Limit Assist rewritten ("the cluster set speed IS the SLA
  target") to fix the dynamic-offset carryover and the activation jerk:
  - Tap-to-adopt activation: with SLA armed (mode = Assist, long engaged —
    no prompts, no auto-activation), a SHORT cruise-down tap activates SLA
    and ADOPTS the current set speed unchanged: going a set 50 mph in a 45
    zone -> active at +11%, zero speed change. The tap is swallowed in
    cruise.py so it no longer also decrements the set speed. Long presses
    remain ordinary speed adjustments and never activate SLA.
  - Offset %% carryover between zones, working: 60 set in a 50 zone = +20%;
    a 30 zone becomes 36. Manually dropping to 33 re-locks the ratio at
    +10%; the next 40 zone becomes 44. ROOT CAUSE FIX: the old cruise
    helper snapped the set speed to the RAW limit on every zone change
    (it never knew the ratio), and SLA then recomputed the ratio from that
    snapped value -> ratio wiped to ~0 at every zone boundary. The helper
    now snaps to limit*(1+ratio) (reads slaDynamicOffset from
    longitudinalPlanSP), which makes SLA's recompute-from-cluster
    idempotent — manual button taps and our own snaps use one code path.
  - Deactivation only on longitudinal disengage or turning the mode off
    (ratio resets). Losing the speed limit source holds the last known
    zone. The preActive confirm flow, CST thresholds and pending state are
    gone; the SLA gas-gating accel path (dead since 3.2.5st — published
    but never consumed) is removed per the single-authority rule: SLA is
    speed-domain only, braking into a lower zone is the MPC's job.
  - tests: test_speed_limit_assist.py rewritten import-light (17 cases,
    including both examples above, snap idempotency, clamp at +/-50%,
    long-press/stale-tap rejection).
* fix(SCC-V, SCC-M): both v2 curve controllers were DEAD CODE — they have
  never activated. SCC-V sampled the removed `lateralPlan` service (the
  read threw every frame; the handler returned 0 predicted lateral accel),
  and SCC-M parsed MapTargetVelocities as {v, dist, radius} when mapd
  writes [{latitude, longitude, velocity}, ...]. The only live curve logic
  was the legacy v1 SCC, whose output the LongV2 governor discarded.
  Rewritten:
  - SCC-V reads modelV2 (orientationRate.z x velocity.x, the proven V-TSC
    signal) and does pointwise corner-speed planning over the plan
    horizon: for each point, corner speed v*sqrt(a_lat_limit/a_pred) plus
    a 1.2 m/s^2 approach-decel budget scaled by time-to-corner. The
    braking point falls out of the math — no ENTERING/TURNING tier
    machine. In-corner it holds corner speed until the plan flattens.
  - SCC-M parses the real mapd route data (nearest-point + forward slice,
    vectorized haversine, 400 m lookahead) and applies a constant-decel
    envelope sqrt(v_curve^2 + 2*a*d) with a 2 s early-arrival buffer —
    replacing jerk-integral braking math that also had a broken quadratic
    root (`/ 2 * a` multiplied by a/2). Straight-road map points are
    never trimmed into constraints.
  - shared long_v2/curve_cap.py: debounced activation (2 frames), cap
    seeded at current speed (no step), fast down-tracking, 2.5 m/s^2-rate
    release, clean deactivation. Both controllers are speed-domain
    governors only — the MPC + shaper own the actual deceleration.
  - honest physics: lat-accel target is now a comfort constant (2.4
    m/s^2, LongV2Tuning `a_lat_target`) with the friction estimate
    bounded to +/-30% influence — `liveParameters` friction is a steering
    -model parameter, not road grip; the old k*fric*g formula demanded
    5.6 m/s^2 lateral before acting. New `sccm_speed_trim` (0.95) trims
    mapd curve speeds directly. k_sccv/k_sccm remain parseable but dead.
  - controllers now honor the SmartCruiseControlVision/Map toggles; the
    legacy v1 SCC (computed, discarded) is removed from the SP planner.
  - tests: new long_v2/tests/test_scc_v2.py (16 cases: activation
    envelopes, approach tightening, in-corner hold, release, passed-curve
    and no-data handling, bounded fric influence, cap seeding/debounce).
* chore: FUNNYPILOT_VERSION -> 3.2.6e; /api/diagnostics EXPECTED_VERSION ->
  3.2.6e, new code_longshape / code_longplan / code_sla / code_sccv2
  self-checks.

FunnyPilot v3.2.5st (2026-07-04)
========================
Root-cause fix for "interpolation feels disabled after the device sits offroad,
reboot doesn't help, only a web-page reflash (~9MB) brings it back".

* fix: The stock openpilot updater was SILENTLY REVERTING the flashed code. The
  chain: the web-UI flash does a plain `git checkout` in /data/openpilot but
  never updates the updater's `UpdaterTargetBranch` param, which still points at
  an older branch from a previous install. While the device sits offroad,
  `updated` runs every ~1.5 h; on a metered hotspot it skips fetching UNTIL its
  3-day timer expires (why it only happens after sitting long enough), then it
  git-fetches the stale target branch (the same ~9MB class of traffic seen on a
  reflash), checks it out in an overlay, and "finalizes" it into
  /data/safe_staging. On the next soft-off -> boot, launch_chffrplus.sh swapped
  /data/openpilot for that finalized copy WITH NO BRANCH CHECK — interpolation
  gone. Reboots can't fix it (the wrong code is now what's installed); a reflash
  fixes it only because it resets the code and its .git mtimes block the swap
  until the updater re-arms, so the cycle repeated. Also explains why "auto
  update off" didn't help: the sunnypilot "Disable Updates" toggle only persists
  if the reboot dialog is CONFIRMED (cancel silently reverts it), and even a
  pre-staged update would still have been installed by the boot swap.
  Fixed in three independent layers (any one alone stops the revert):
  - launch_chffrplus.sh — boot-time branch guard: a finalized staged update is
    only installed if it is on the SAME git branch as the currently flashed
    code; otherwise it is discarded (`.overlay_consistent` removed) and a
    message is logged. Branch switches now only ever happen via an explicit
    flash (web UI or ssh), never via the boot swap. NOTE: this intentionally
    makes the sunnypilot settings-menu branch selector unable to switch
    branches on this fork — use the web UI Flash button instead.
  - system/updated/updated.py — target self-heal: on startup, if the checked
    out branch is a funnypilot-* branch and `UpdaterTargetBranch` differs, the
    param is rewritten to the flashed branch, so the updater can only ever
    stage the code that is already installed. Also, if the target branch does
    not exist on the `origin` remote, the fetch is skipped cleanly instead of
    failing forever (which would eventually raise the connectivity-needed
    offroad alerts that can block engagement).
  - sunnypilot/navd/nav_webserver.py — the /api/flash endpoint now writes
    `UpdaterTargetBranch` to the branch being flashed and wipes
    /data/safe_staging (unmounting the updater overlay first) before rebooting,
    so a previously staged wrong-branch update can't be installed by the very
    reboot the flash triggers.
* feat: /api/diagnostics ("Verify" button) now surfaces the revert vector:
  `updater_target` FAILS if UpdaterTargetBranch points at a different branch,
  `staged_branch` WARNS if a different branch is staged in /data/safe_staging,
  `updater_off` reports whether DisableUpdates is actually set (catches the
  cancelled-reboot-dialog trap), and new `code_bootguard` / `code_updtarget`
  self-checks verify the two guards are present in the running code.
* fix: "Working tree unmodified" Verify check no longer fails on the on-device
  runtime artifacts `.nav_secrets` and `.funnypilot_nav_cache` — they are not
  shipped code and are now listed in .gitignore, so `git status --porcelain`
  (what the check runs) ignores them.
* chore: FUNNYPILOT_VERSION 3.2.3st -> 3.2.5st (3.2.4e never bumped the file);
  /api/diagnostics EXPECTED_VERSION/branch checks -> 3.2.5st. No control-path
  changes: the 3.2.4e interpolation/smoothing (lat_interp SETTLE + jerk-limited
  model action smoothing) carries over byte-identical.

FunnyPilot v3.2.3st (2026-06-26)
========================
Stable cut of 3.2.2 with three follow-up fixes from on-road feedback.

* fix: Smooth lane changes restored. The v3.2.2 SETTLE ease-out leads the curvature
  as it flattens, which sharpened the S-shaped lane-change path and made lane
  changes feel sharp/jerky vs the smooth 3.2.1st feel. SETTLE is now forced OFF
  during a lane change (model laneChangeState != off) so the maneuver uses the
  plain linear ramp again — identical to 3.2.1st. Normal-driving SETTLE smoothing
  is unchanged, and the soft-lane-change TORQUE scaling in latcontrol_torque.py was
  always untouched. (selfdrive/controls/lib/lat_interp.py — new `lane_change` arg;
  controlsd.py passes it.)
* feat: Driver-override softening — easier to retake the wheel by hand. The v3.2.2
  interpolation sends firmer, more consistent curvature commands, so manually
  pushing the wheel away met more resistance. When the driver is actively applying
  torque (CS.steeringPressed), the TOTAL output torque now ramps down to 60%
  (_OVERRIDE_MIN_SCALE), via a FirstOrderFilter so there's no step in or out, and
  an exact no-op (100%) when not pressing. The interpolation/smoothing is untouched
  and the panda's hardware torque limits remain the safety backstop. Tune
  _OVERRIDE_MIN_SCALE in latcontrol_torque.py if you want lighter/heavier override.
* fix: Brake-with-lead disengage chime silenced (2021 Kia K5 GT, STOCK
  longitudinal). Root cause: the K5 is a classic-CAN button-cancel car, and when
  you brake to disengage, the brake pedal ALREADY cancels the factory cruise — but
  openpilot also spams a redundant CLU11 CANCEL, and doing that while the SCC was
  following a lead is what makes the car chime (no lead -> no chime, matching the
  report). The redundant CANCEL is now suppressed WHILE the brake is pressed; the
  instant the brake releases, CANCEL resumes if cruise is somehow still enabled, so
  cruise can never get stuck engaged. (opendbc_repo/.../hyundai/carcontroller.py)
  VERIFY ON-DEVICE: confirm braking still reliably cancels cruise (it does so via
  the pedal natively); if the chime persists it is coming from a different layer —
  report back and we'll re-trace with a CAN log.
* chore: FUNNYPILOT_VERSION -> 3.2.3st; /api/diagnostics EXPECTED_VERSION/branch
  bumped, plus new `code_override` and `code_chime` self-checks and `code_controlsd`
  now greps `v3.2.3st`. New test_lane_change_forces_linear in test_lat_interp.py.

FunnyPilot v3.2.2 (2026-06-26)
========================
* fix: Interpolation no longer silently degrades after offroad/reboot. Root cause:
  the old interpolation COUNTED control frames and assumed exactly 5 per model
  frame (100 Hz / 20 Hz). When controlsd's effective rate drifts toward the model
  rate under thermal/CPU load — which builds up the longer the device runs, i.e.
  exactly after an offroad/reboot cycle rather than after a cool fresh flash — the
  frame counter stopped advancing and the steering command FROZE at ~20% of every
  model step (the "big bites / feels deactivated" symptom). The interpolation is
  now TIME-ANCHORED: the sub-frame phase is real elapsed wall-clock time over the
  model's fixed 20 Hz period, so it reaches the model's desire on time at any loop
  rate and can't stall. A health term blends toward the model's raw desire when
  sub-frame headroom is lost, so the worst case degrades to STOCK openpilot —
  never to a laggy stall. The healthy-100 Hz feel is bit-compatible with 3.1.0e+.
  (new selfdrive/controls/lib/lat_interp.py; selfdrive/controls/controlsd.py)
* feat: New SETTLE interpolation method, now the DEFAULT. Full responsiveness INTO
  a corner (mathematically never below the linear path — no added turn-in lag),
  then a gentle ease-out as the model's desired curvature flattens toward the
  apex/exit, so the wheel SETTLES instead of arriving in a 20 Hz jerk impulse
  (the "whiplash"). The "is it flattening?" decision uses a one-model-step
  lookahead sampled from the model's OWN published plan (orientation /
  orientationRate) — free compute available inside the actuator/software-delay
  window. Guaranteed to never sit outside the model's [prev, cur] desire bracket
  and to equal the model exactly at each model frame (the model stays the
  reference). Falls back to linear below ~15 mph and on any invalid lookahead.
  Set `INTERP_METHOD = LINEAR` in controlsd.py for the plain validated delta/5
  feel (A/B by flashing, per the fork's pin-in-code convention).
* feat: Honest INTERP health indicator. controlsd writes the REALIZED
  control-frames-per-model-frame to /dev/shm/lat_interp (≈5 healthy, lower =
  losing sub-frame headroom, 0 = paused) instead of a constant "5" heartbeat.
  The dev-UI element colors it green ≥4 / orange 2–3 / red 1, so a degradation is
  now VISIBLE on-device instead of masked by an always-green "5".
  (selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py)
* test: New selfdrive/controls/lib/tests/test_lat_interp.py proves the safety
  invariants (in-bracket / never outside model desire, reaches cur at the model
  frame, SETTLE never below linear, linear == old delta/5 schedule, cadence
  independence, degraded-beats-old-20%-stall, NaN/re-engage/low-speed contained).
* note: A SECOND, device-side cause of "works after reflash, reverts after offroad"
  is possible and is NOT fixable in this repo: the openpilot updater can fetch a
  different `UpdaterTargetBranch` over offroad wifi and swap it in on reboot.
  Confirm on-device with `cat /data/params/d/UpdaterTargetBranch` and
  `grep -c v3.2.2 /data/openpilot/selfdrive/controls/controlsd.py` (the web-UI
  "Verify" button now checks both the version and the new lat_interp module). If
  the running code isn't 3.2.2 after a reboot, the updater reverted it — re-flash
  funnypilot-3.2.2 and check that the updater target branch matches.
* chore: FUNNYPILOT_VERSION → 3.2.2; /api/diagnostics EXPECTED_VERSION, branch
  check, and code markers updated (controlsd grep `v3.2.2`, new `lat_interp`
  module check) so the on-device self-check stays green on this branch.

FunnyPilot v3.2.1st (2026-06-21)
========================
* tweak: More aggressive post-blinker lateral re-engage (stable cut of 3.2.1e).
  - Settle hold shortened 1.0 s → 0.67 s: after the blinker turns off, lateral
    resumes once the wheel has been held within ±20° of center (threshold
    unchanged) for 0.67 s instead of 1.0 s. Brief center crossings still reset the
    timer. (sunnypilot/.../blinker_pause_lateral.py — UNWIND_SETTLE_TIME)
  - Re-engage torque ramp now starts at 15% (was 0%) and reaches 100% over 3 s
    (was 4 s), so steering authority returns sooner without snapping to full
    torque. (latcontrol_torque.py — _REENGAGE_RAMP_START, _REENGAGE_RAMP_DUR)
* note: Stable release based on funnypilot-3.2.1e. All other 3.2.1e behavior —
  fixed 5-way interpolation + INTERP heartbeat and the "Verify" diagnostics modal —
  carries over unchanged. FUNNYPILOT_VERSION → 3.2.1st; /api/diagnostics
  EXPECTED_VERSION and branch check updated to match so the on-device self-check
  stays green on the stable branch.

FunnyPilot v3.2.1e (2026-06-20) [EXPERIMENTAL]
========================
* change: Interpolation is now FIXED at 5-way uniform slicing, unconditionally.
  The dynamic-n corner-intensity gauge and the dCRV (delta-curvature) machinery
  were removed entirely. The control feel is unchanged from 3.1.x (it was always
  uniform delta/5); this just deletes the now-dead gauge math. _MP_MAX_DELTA /
  _MP_HOLD_DECAY / n_raw / n_held are gone.
* change: dev-UI "dCRV" element removed. "INTERP" now reads a constant 5 (green)
  while lateral is engaged and 0 (white) when the interpolation path is paused —
  it is now a live "interp alive" indicator, written to /dev/shm/lat_interp at the
  20 Hz model rate as a heartbeat (useful for spotting if interp ever stops).
* feat: Blinker-unwind re-engage torque ramp. When the blinker-pause feature
  releases lateral control, total torque ramps CONTINUOUSLY 0% → 100% over 4 s —
  recomputed every 100 Hz control frame as a smooth linear function of elapsed
  time (not stepped quarters) — instead of snapping to full authority. Scoped to
  blinker pauses only (a blinker seen while inactive) — a plain engage or
  standstill release still gets instant authority. (latcontrol_torque.py)
* feat: Web UI "Verify" button — a one-tap code-verification modal (styled like a
  test suite, PASS/FAIL/WARN pills) that runs read-only on-device checks (git
  branch/HEAD, working-tree-unmodified, our code markers present, INTERP heartbeat,
  model bundle, updater staging/overlay) and a "Copy Output" button to paste the
  report straight back into Claude. New POST /api/diagnostics endpoint.
  (sunnypilot/navd/nav_webserver.py, sunnypilot/navd/nav_web/index.html)
* fix: Blinker-unwind no longer re-engages mid-S-curve. The wheel must now stay
  within ±20° of center CONTINUOUSLY for 1.0 s (UNWIND_SETTLE_TIME) before lateral
  resumes; briefly passing through center on the way to the opposite lock resets
  the timer. (sunnypilot/.../blinker_pause_lateral.py)
* note: Suspected cause of "interp feels like it deactivates after a few days"
  documented for on-device diagnosis (likely the openpilot updater touching the
  working tree on nightly wifi, or a model-bundle change). The new INTERP
  heartbeat helps confirm whether the interp path is live.

FunnyPilot v3.1.2 (2026-05-31)
========================
* tweak: INTERP gauge tuning (display only — control still uniform delta/5).
  - Threshold _MP_MAX_DELTA 0.00006 → 0.000051 (15% lower), so a 15% gentler
    curve now registers the same INTERP value as before.
  - Default/resting INTERP raised to 3 (n floor 1 → 2).
  - Max INTERP raised to 10 (n cap 6 → 9).
  - Colors rescaled for the 3–10 range: green 3–5, orange 6–8, red 9–10.
  NOTE: Since 3.1.0e the INTERP number does not size the steering control
  (that is always uniform delta/5); these changes affect the readout only.

FunnyPilot v3.1.1st (2026-05-31)
========================
* tweak: INTERP gauge now ranges 1–7 (was 1–5). n_interp cap raised 4 → 6 so
  sharp curves register the full range instead of saturating. Colors: white = 1,
  green = 2–4, orange = 5–7. (Display gauge only — control still uses uniform
  delta/5 slicing from 3.1.0e.)
* tweak: dCRV display scaled ×100 — 0.000637 now reads "0.0637" instead of
  "0.0006". Drops two wasted leading zeros and surfaces two more digits of
  resolution within the same width.
* Stable release built on the validated 3.1.0e uniform-interpolation feel.

FunnyPilot v3.1.0e (2026-05-31) [EXPERIMENTAL]
========================
* fix: Interpolation now actually slices finely. Root cause of "still feels like
  big infrequent bites": the dynamic-n schedule, in the common case (n_interp=1,
  shown as "2" in the UI), built [prev, prev+0.5·delta, cur, cur, cur] — i.e. two
  coarse half-delta jumps in 20 ms then 30 ms flat. The dev-UI value was correct
  but the control was only using 2 of 5 frames at half-steps.
  Control now ALWAYS spreads each model step uniformly across all 5 control frames
  (delta/5 per frame), reaching the new target at the final sub-frame. 2.5x finer
  steering steps in normal driving for the same ~zero added lag.
* note: The dynamic n_interp value is retained purely as the dev-UI corner-intensity
  gauge (it no longer sizes the control steps). _MP_MAX_DELTA / _MP_HOLD_DECAY now
  only affect that display.

FunnyPilot v3.0.9e (2026-05-31) [EXPERIMENTAL]
========================
* feat: Soft lane changes — scale the TOTAL steering torque (feedforward +
  correction) during a lane change instead of only the PID correction. The
  previous design kept the feedforward at 100%, but the lane-change motion
  itself lives in the feedforward, so the maneuver was never actually softened
  — only the error tracking around it. Now the whole motion eases in.
* feat: Floor of 45% — total torque never drops below 45% of demand during the
  maneuver, so corners aren't lost if you signal mid-curve. Blinker ON ramps
  45% → 100% over 6 s; blinker OFF holds 45% for 0.5 s then 45% → 100% over 2 s
  (alpha² ease-in). The old 0% dead zone is removed (it would mean zero steering
  authority under total scaling).
* refactor: Removed the FF/correction split entirely (and with it the NNLC
  torque-space special-casing from 3.0.8e) — no longer needed with total scaling.

FunnyPilot v3.0.8e (2026-05-31) [EXPERIMENTAL]
========================
* fix: Lane change torque split now preserves the ACTUAL feedforward. The split
  previously isolated the "correction" by subtracting a linear-feedforward torque
  even when NNLC produced the output from its own (different) neural feedforward.
  During a lane change at low scale this swapped most of the neural feedforward
  for the linear one — invisible behind a lead (the two agree in steady following)
  but rough on open road (they diverge), which is exactly why lane changes felt
  smooth only with a lead. Now decomposes via the PID's own F vs P+I+D in native
  units, correct for both the neural (torque-space) and linear (lat-accel) paths.
  Exact no-op at scale 1.0, so normal driving is unchanged.

FunnyPilot v3.0.7 (2026-05-31)
========================
* feat: Interpolation peak-hold with decay. n_interp now snaps up instantly on
  a large model-desire delta but decays slowly (~0.15 levels/gate, 5→2 over ~1s)
  instead of dropping the instant the per-gate delta shrinks. Keeps fine
  interpolation through the body of a corner, not just at entry — fixes the
  "interp drops back to 2 while I'm still mid-corner" feel.
* tweak: Kept the model-desire delta as the interpolation trigger rather than
  measured steer angle. The model desire is forward-looking (pre-compensated for
  lat_delay); measured angle is the lagged response and would react to bites that
  already happened. Decay-hold addresses the "feels delayed" symptom instead.
* tweak: INTERP developer UI value now displays a 1-second rolling peak (matching
  dCRV) so the value it's hitting is readable instead of flickering.

FunnyPilot v3.0.6 (2026-05-31)
========================
* tune: _MP_MAX_DELTA 0.000033 → 0.00006 (midpoint between 3.0.4e and 3.0.5e).

FunnyPilot v3.0.5e (2026-05-31) [EXPERIMENTAL]
========================
* fix: Replace speed-scaled torque threshold with a fixed curvature threshold
  of 0.0001 rad/m per step. The v² denominator was making max_step larger than
  the observed deltas at city speeds, causing n_interp to stay at 1 regardless
  of curve sharpness. With the fixed threshold, display climbs 2→5 across the
  observed delta range (~0.0001 straight, ~0.0004 sharp curve).
* fix: Replace Unicode Δ with ASCII "dCRV" in developer UI — Δ rendered as
  "?" due to missing glyph in the on-device font.

FunnyPilot v3.0.4e (2026-05-31) [EXPERIMENTAL]
========================
* tune: _MP_MAX_STEP 0.10 → 0.04. Lower threshold means n_interp climbs
  sooner and more often — display will show 3–5 on moderate curves rather
  than staying at 2 for most of the drive.

FunnyPilot v3.0.3e (2026-05-31) [EXPERIMENTAL]
========================
* feat: Dynamic interpolation step count. Steps per model gate scales with
  the curvature delta — each step bounded to ≤10% torque-equivalent
  (speed-scaled via LAF=2.750). 5-value schedule pre-built per gate.
  n=0 → display 1 (direct jump). n=1 → display 2 (normal). n=4 → display 5.
* feat: INTERP N in developer UI bottom bar (torque controller only).
  IPC via /dev/shm/lat_interp written by controlsd at model-gate rate.
  Green=2–3, Orange=4–5, White=1. Zero compilation required.
* tweak: Post-blinker correction ramp is now quadratic ease-in (alpha²).

FunnyPilot v3.0.2e (2026-05-28) [EXPERIMENTAL]
========================
* fix: Removed Layers 1 and 2 from lateral controller — the FirstOrderFilter
  on feedforward and the setpoint averaging window both introduced phase lag
  that caused outward drift on curves. Setpoint lookup restored to direct
  single-point: lat_accel_request_buffer[-delay_frames].
* feat: Midpoint interpolation in controlsd. Between consecutive model frames
  (20 Hz) a single mid-frame command is injected at controlsd frame index 2 of 5
  (10 ms after the model frame) equal to (prev_frame + cur_frame) / 2. Effective
  lateral command rate rises from 20 Hz to ~40 Hz with zero added lag: no filter,
  no phase shift — just geometry. When latActive is False the state resets so
  there is no stale value on re-engagement.

FunnyPilot v3.0.1 (2026-05-28)
========================
* fix: latAccelFactor (LAF) locked at 2.750. Live torque calibration updates
  latAccelOffset and friction normally but LAF is pinned, giving consistent
  steering feel regardless of torqued's current estimate.
* tweak: Smooth stopping interpolation confirmed 0–15 mph (100% torque at 15 mph).
* feat: Web UI branch selector rebuilt as a native scrollable list — no more
  native <select> overflowing the screen on mobile. Bottom-sheet modal, touch
  targets ≥44 px, minimal dark theme. Flash button disabled until a branch is
  selected; status feedback inline.

FunnyPilot v3.0.0e (2026-05-28) [EXPERIMENTAL]
========================
* note: LAT_SMOOTH_SECONDS held at 0.0 (reverted from 0.1) pending validation —
  the shared constant has complex interactions with NNLC's desired_lat_jerk_time
  that need further testing before enabling.
* exp: Layer 1 — Feedforward smoother. The curvature-driven feedforward term
  (path/corner demand passed to the PID) is passed through a FirstOrderFilter
  with time constant dynamically set to max(lat_delay, 0.1 s) each frame. Sudden
  path model updates are spread over the vehicle's own response window. Friction
  compensation is added AFTER the filter so it remains fully responsive to
  direction changes. Filter is seeded from current ff on re-engagement to prevent
  torque spikes.
* exp: Layer 2 — Setpoint averaging. The single delay-point lookup is replaced
  with a mean over a ±(delay/3) window centered on the delay point. Single-frame
  error spikes are suppressed without time-shifting the setpoint.
* Sanity-checked: DM-disabled forceDecel defaults to 0.0 (safe), isRHD defaults
  to False (correct for US/LHD). No blocking issues found.

FunnyPilot v3.0.0 (2026-05-28)
========================
* feat: Driver monitoring silenced — processes kept running (required for UI
  data flows and driverStateV2 stability) but all timeouts set to 86400 s (24 h)
  so DM never reaches alert state. Events also blocked at selfdrived level as a
  second layer of protection. No beeping, no alerts, no disengagement.
* feat: Corner-aware lane change torque. Output torque is now split into
  feedforward (corner demand) and correction (PID error) components. Only the
  correction is reduced during a lane change; the feedforward always runs at
  100%, ensuring the car never applies less torque than the curve requires and
  cannot slip to the outside of a turn. Correction starts at 10% on blinker
  onset and ramps linearly to 100% over 6 seconds.
* feat: Post-blinker settle sequence. When the blinker turns off, correction
  torque drops to 0% for a 0.5 s dead zone (letting the steering unwind
  without controller fight), then ramps from 0% back to 100% over 2 seconds,
  preventing the abrupt full-torque snap back to lane-center the moment the
  wheel realigns.

FunnyPilot v2.0.5 (2026-05-08)
========================
* tweak: Reintroduced the hidden −10% cruise offset strictly when cruise is the
  sole limiter, leaving UI and PCM speeds untouched while smoothing positive
  acceleration transients via a first-order filter.
* tweak: Lane change torque ramp now preserves the torque present at signal
  onset, preventing understeer on curved roads while still easing back to full
  authority over five seconds.
* fix: Follow distance tables expanded ~50% across all personalities, with
  inflated lead obstacles and longer stop distance for safer approach behavior.
* fix: Improved blend between free-cruise and lead-governed targets to remove
  longitudinal oscillations when a lead drops in and out of view.
* fix: Experimental-mode cruise initialization now honors current vehicle speed
  instead of defaulting to 65 mph.
* fix: Speed Limit Assist keeps the user-selected offset across zones and begins
  gas gating earlier with stronger decel bias to meet new limits smoothly.

FunnyPilot v2.0.4 (2026-05-08)
========================
* revert: Longitudinal planner, MPC tuning, and LongV2 controllers restored to
  the v2.0.0 state after regression reports in 2.0.1/2.0.2.
* fix: Web terminal now spawns the user's login shell (outside the venv) while
  automatically removing `VIRTUAL_ENV` so Git automation works without manual
  cleanup.
* fix: SCC-V / SCC-M debug badges always render their governing speed in MPH
  when Developer UI is enabled, even when the controllers are inactive.

FunnyPilot v2.0.3 (2026-05-08)
========================
* fix: Cruise offset now only applies when the planner is free-cruising on the
  user set speed. Lead vehicles, map caps, and speed limits retain full
  authority, preventing runaways once external constraints clear.
* fix: Browser terminal launches outside the openpilot virtualenv and flash
  scripts execute with a clean PATH, so git branch discovery and updater flows
  work reliably again via the web UI terminal.

FunnyPilot v2.0.2 (2026-05-06)
========================
* fix: Critical safety bug — runaway acceleration with no lead vehicle
  Root cause: V_CRUISE_UNSET (255 kph) is the sentinel for "cruise not
  set yet." min(255, V_CRUISE_MAX=145) = 145 kph = 90 mph was flowing
  through to the MPC uncapped because the -10% offset only applied when
  initialized. MPC then chased 90 mph with no lead to constrain it.
  Fix: when v_cruise_initialized = False, use v_ego (hold current speed)
  instead of V_CRUISE_MAX.
* fix: Remove lead-lost holdout from FollowingControllerV2. The 1.2s
  hold + 2.0s ramp on lead disappearance caused edge cases on cold-start
  (no lead → immediate cap at v_ego=0 for several seconds). The MPC
  handles lead-loss transitions naturally via its own safe-distance
  constraint. When no lead is detected, all caps/overrides clear instantly.

FunnyPilot v2.0.1 (2026-05-06)
========================
* Lead following — closing-rate brake curve:
  - Replaced TTC-only tier triggering. With small Δv (e.g. 50→42 mph) the
    old TTC stayed >10s until you were right on top of the lead, so the
    controller did nothing and the MPC coasted too late and too long.
  - New: required-decel tiers based on `(v_ego² − v_lead²) / (2·Δd_to_gap)`.
    Pre-empts the coast and engages gentle decel as soon as we're closing.
* Stronger stopping power:
  - LongV2 `decel_comfort` 1.8 → 2.5 m/s², added `decel_max` 3.5 m/s² for tier 4
  - `jerk_limit_normal` 0.5 → 0.7 m/s³, `jerk_limit_safety` 3.0 → 4.5 m/s³
  - MPC `COMFORT_BRAKE` 2.0 → 1.5: enlarges safe-distance constraint so MPC
    starts braking sooner when closing on a slow lead
* SCC badges in debug UI mode:
  - When DevUIInfo is enabled, badges show the calculated v_target at all
    times (not only when actively governing). SCCVisionV2 / SCCMapV2 now
    always publish their computed corner speed.
* Friction coefficient recalibration (matches observed 0.08 — 1.1 range):
  - Nominal dry pavement μ = 0.5 (was 0.8)
  - Wet/snow boundary at μ = 0.092 (was 0.6)
  - `comfort_scale` linear ramp from 0.5 (wet floor) to 1.0 (dry nominal)
  - `weather_speed_scale` ramps 0.6 → 1.0 across the wet range
* Hidden −10% cruise speed offset:
  - The speedometer / set-speed UI is unchanged (reads `carState.vCruise`
    directly), but everything downstream of the planner sees `v_cruise * 0.9`.
  - Setting cruise at 50 mph → MPC plans for 45 mph.
* Post-blinker unwind (ported from 1.0.9):
  - Lateral control stays paused after the blinker turns off until the
    steering wheel returns within 20° of center.
  - Hardcoded ON for this personal branch (no params toggle).
* Driver monitoring restored to 9× timeouts (matches 1.0.8.3):
  - Passive wheel-touch: 270s (was 90s)
  - Active monitoring: 99s (was 33s)
* Smooth lane change torque (blinker-triggered):
  - On blinker rising edge, lateral torque drops to 25% (75% reduction) and
    linearly ramps back to 100% over 5.0 seconds.
  - Trigger is purely the blinker — ramp begins the moment the user signals,
    not when the model commits curvature.

FunnyPilot v2.0.0 (2026-05-06)
========================
* LongV2 — physics-based longitudinal control:
  - FRIC-aware corner speed: v_corner = k × sqrt(μ × g × R) using live friction coefficient
  - SCC-Vision v2: threshold-based on k_sccv × FRIC × g, p97 predicted lateral accel
  - SCC-Map v2: physics cross-validation of mapd speed targets; corner unwind via curvatureRates
  - Speed governor: min(v_cruise, v_scc_map, v_scc_vision, v_sla, v_road_cap, v_weather_cap)
  - Following v2 state machine: CRUISE/GAP_ACQUIRING/FOLLOWING/DECELERATING/STOPPING/STOPPED/REACCEL
  - Jerk filter: 0.5 m/s³ normal, 3.0 m/s³ safety, per-tier override from following controller
  - Weather speed cap: activates when FRIC < 0.6 (wet road detection)
  - All constants externalized to Params["LongV2Tuning"] JSON (no recompile needed)
* SCC badge UI redesign:
  - Green (inactive) → Orange (gas gating) → Red (braking): 300ms smooth color transition
  - When governing, badge shows speed in MPH instead of label
  - Drop shadow, auto-width pill shape
* Terminal server (port 8888):
  - PTY-backed bash shell accessible from any browser at http://<device-ip>:8888
  - Flash & Reboot modal: lists all funnypilot branches sorted by last commit date
  - Auto-reconnects after reboot

FunnyPilot v0.9.8 (2026-03-01)
========================
* Dynamic Speed Limit Assist (Locked Mode):
  - SLA now stays permanently locked once activated (only clears on cruise disengage)
  - When user adjusts cruise while SLA is active, records the offset % relative to limit
    (e.g. 36mph in a 30mph zone = +20% offset)
  - On entering a new speed limit zone, the stored offset is automatically applied
    (e.g. +20% offset + 40mph zone = 48mph effective target)
  - Offset is continuously updated whenever the user adjusts cruise
  - Offset is capped at ±50% for safety
  - Works for automatic speed reductions (SLA slows car to new_limit * (1 + offset))
  - For increases: SLA sets target above current cruise; user confirms with + press
* Dynamic SLA status badge in onroad UI:
  - Teal "SLA" badge appears near the speed limit sign when locked
  - Shows current dynamic offset (e.g. "+20%", "-5%", "±0%")
* Revised follow distance (all distances recalibrated):
  - Distance 2 (standard): 2.5s@≤20mph, gradient 20-35mph, 1.5s@35-50mph, gradient 50-75mph, 1.0s@≥75mph
  - Distance 1 (aggressive): 15% shorter than standard at all speeds
  - Distance 3 (relaxed): 15% longer than standard at all speeds

FunnyPilot v0.9.7h (2026-02-25) HOTFIX
========================
* Fixed plannerd crash on long control enable: empty modelV2 arrays caused ValueError in
  SCC-V gas gating (np.amax/np.percentile on zero-size array); added length guard before
  all numpy reductions in vision_controller.py
* Fixed latcontrol_torque.py: moved `import time` out of hot update() loop to module level

FunnyPilot v0.9.7 (2026-02-25)
========================
* Rebased on sunnypilot v2026.001.000 (2026-02-24)
* Gas gating UI indicator: SCC-V and SCC-M badges turn orange with "GAS GATE" label when active
* Variable follow distance (speed-dependent): closer at highway speeds, more buffer in town
  - Distance 1 (aggressive): 2.5s@<20mph, 1.2s@45mph, 0.75s@75mph+
  - Distance 2 (standard): 3.0s@<20mph, 1.6s@45mph, 1.0s@75mph+
  - Distance 3 (relaxed): 3.8s@<20mph, 2.2s@45mph, 1.4s@75mph+
* Follow distance switching gas gates instead of braking (4s gas gate on distance increase)
* High speed warning now a silent static banner (no audio, no disengage, no NO_ENTRY block)
* Speed limit assist now auto-tracks zone changes when active (no confirmation needed)
  - Manual cruise speed change deactivates SLA until next speed limit zone change
  - Re-prompts for confirmation after each manual override, then auto-tracks again
* New longitudinal tuning:
  - COMFORT_BRAKE reduced 2.5->2.0 m/s² (earlier, gentler braking)
  - STOP_DISTANCE increased 6.0->8.5m (more buffer at stops)
  - Raw aLeadK preserved for fast stoplight reaction (not smoothed)
  - Smoothed dRel and vLeadK for stable tracking
* Lane change torque ramp: 3.5s ramp, starting at 40% (carried from v0.9.6h)
* Smooth stopping: linear torque reduction below 15mph (carried from v0.9.6)
* Extended driver monitoring timeouts: 3x original values (carried from v0.9.6)
* Max acceleration capped at 70% of openpilot defaults (carried from v0.9.6)

sunnypilot Version 2026.001.000 (2026-03-xx)
========================
* What's Changed (sunnypilot/sunnypilot)
  * Complete rewrite of the user interface from Qt C++ to Raylib Python
  * comma four support
  * ui: sunnypilot toggle style by @nayan8teen
  * ui: fix scroll panel mouse wheel behavior by @nayan8teen
  * ui: sunnypilot panels by @nayan8teen
  * sunnylink: centralize key pair handling in sunnylink registration by @devtekve
  * ui: reimplement sunnypilot branding with Raylib by @sunnyhaibin
  * ui: Platform Selector by @Discountchubbs
  * ui: vehicle brand settings by @Discountchubbs
  * ui: sunnylink client-side implementation by @nayan8teen
  * ui: `NetworkUISP` by @Discountchubbs
  * ui: add sunnypilot font by @nayan8teen
  * ui: sunnypilot sponsor tier color mapping by @sunnyhaibin
  * ui: sunnylink panel by @nayan8teen
  * ui: Models panel by @Discountchubbs
  * ui: software panel by @Discountchubbs
  * modeld_v2: support planplus outputs by @Discountchubbs
  * ui: OSM panel by @Discountchubbs
  * ui: Developer panel extension by @Discountchubbs
  * sunnylink: Vehicle Selector support by @sunnyhaibin
  * [TIZI/TICI] ui: Developer Metrics by @rav4kumar
  * [comma 4] ui: sunnylink panel by @nayan8teen
  * ui: lateral-only and longitudinal-only UI statuses support by @royjr
  * sunnylink: elliptic curve keys support and improve key path handling by @nayan8teen
  * sunnylink: block remote modification of SSH key parameters by @zikeji
  * [TIZI/TICI] ui: rainbow path by @rav4kumar
  * [TIZI/TICI] ui: chevron metrics by @rav4kumar
  * ui: include MADS enabled state to `engaged` check by @sunnyhaibin
  * Toyota: Enforce Factory Longitudinal Control by @sunnyhaibin
  * ui: fix malformed dongle ID display on the PC if dongleID is not set by @dzid26
  * SL: Re enable and validate ingestion of swaglogs by @devtekve
  * modeld_v2: planplus model tuning by @Discountchubbs
  * ui: fix Always Offroad button visibility by @nayan8teen
  * Reimplement sunnypilot Terms of Service & sunnylink Consent Screens by @sunnyhaibin
  * [TIZI/TICI] ui: update dmoji position and Developer UI adjustments by @rav4kumar
  * modeld: configurable camera offset by @Discountchubbs
  * [TIZI/TICI] ui: sunnylink status on sidebar by @Copilot
  * ui: Global Brightness Override by @nayan8teen
  * ui: Customizable Interactive Timeout by @sunnyhaibin
  * sunnylink: add units to param metadata by @nayan8teen
  * ui: Customizable Onroad Brightness by @sunnyhaibin
  * [TIZI/TICI] ui: Steering panel by @nayan8teen
  * [TIZI/TICI] ui: Rocket Fuel by @rav4kumar
  * [TIZI/TICI] ui: MICI style turn signals by @rav4kumar
  * [TIZI/TICI] ui: MICI style blindspot indicators by @sunnyhaibin
  * [MICI] ui: display blindspot indicators when available by @rav4kumar
  * [TIZI/TICI] ui: Road Name by @rav4kumar
  * [TIZI/TICI] ui: Blue "Exit Always Offroad" button by @dzid26
  * [TIZI/TICI] ui: Speed Limit by @rav4kumar
  * Reapply "latcontrol_torque: lower kp and lower friction threshold (commaai/openpilot#36619)" by @sunnyhaibin
  * [TIZI/TICI] ui: steering arc by @royjr
  * [TIZI/TICI] ui: Smart Cruise Control elements by @sunnyhaibin
  * [TIZI/TICI] ui: Green Light and Lead Departure elements by @sunnyhaibin
  * [TIZI/TICI] ui: standstill timer by @sunnyhaibin
  * [MICI] ui: driving models selector by @Discountchubbs
  * [TIZI/TICI] ui: Hide vEgo and True vEgo by @sunnyhaibin
  * [TIZI/TICI] ui: Visuals panel by @nayan8teen
  * Device: Retain QuickBoot state after op switch by @nayan8teen
  * [TIZI/TICI] ui: Trips panel by @sunnyhaibin
  * [TIZI/TICI] ui: dynamic ICBM status by @sunnyhaibin
  * [TIZI/TICI] ui: Cruise panel by @sunnyhaibin
  * ui: better wake mode support by @nayan8teen
  * Pause Lateral Control with Blinker: Post-Blinker Delay by @CHaucke89
  * SCC-V: Use p97 for predicted lateral accel by @yasu-oh
  * Controls: Support for Torque Lateral Control v0 Tune by @sunnyhaibin
* What's Changed (sunnypilot/opendbc)
  * Honda: DBC for Accord 9th Generation by @mvl-boston
  * FCA: update tire stiffness values for `RAM_HD` by @dparring
  * Honda: Nidec hybrid baseline brake support by @mvl-boston
  * Subaru Global Gen2: bump steering limits and update tuning by @sunnyhaibin
  * Toyota: Enforce Stock Longitudinal Control by @rav4kumar
  * Nissan: use MADS enabled status for LKAS HUD logic by @downquark7
  * Reapply "Lateral: lower friction threshold (#2915)" (#378) by @sunnyhaibin
  * HKG: add KIA_FORTE_2019_NON_SCC fingerprint by @royjr
  * Nissan: Parse cruise control buttons by @downquark7
  * Rivian: Add stalk down ACC behavior to match stock Rivian by @lukasloetkolben
  * Tesla: remove `TESLA_MODEL_X` from `dashcamOnly` by @ssysm
  * Hyundai Longitudinal: refactor tuning by @Discountchubbs
  * Tesla: add fingerprint for Model 3 Performance HW4 by @sunnyhaibin
  * Toyota: do not disable radar when smartDSU or CAN Filter detected by @sunnyhaibin
  * Honda: add missing `GasInterceptor` messages to Taiwan Odyssey DBC by @mvl-boston
  * GM: remove `CHEVROLET_EQUINOX_NON_ACC_3RD_GEN` from `dashcamOnly` by @sunnyhaibin
  * GM: remove `CHEVROLET_BOLT_NON_ACC_2ND_GEN` from `dashcamOnly` by @sunnyhaibin
* New Contributors (sunnypilot/sunnypilot)
  * @TheSecurityDev made their first contribution in "ui: fix sidebar scroll in UI screenshots"
  * @zikeji made their first contribution in "sunnylink: block remote modification of SSH key parameters"
  * @Candy0707 made their first contribution in "[TIZI/TICI] ui: Fix misaligned turn signals and blindspot indicators with sidebar"
  * @CHaucke89 made their first contribution in "Pause Lateral Control with Blinker: Post-Blinker Delay"
  * @yasu-oh made their first contribution in "SCC-V: Use p97 for predicted lateral accel"
* New Contributors (sunnypilot/opendbc)
  * @AmyJeanes made their first contribution in "Tesla: Fix stock LKAS being blocked when MADS is enabled"
  * @mvl-boston made their first contribution in "Honda: Update Clarity brake to renamed DBC message name"
  * @dzid26 made their first contribution in "Tesla: Parse speed limit from CAN"
  * @firestar5683 made their first contribution in "GM: Non-ACC platforms with steering only support"
  * @downquark7 made their first contribution in "Nissan: use MADS enabled status for LKAS HUD logic"
  * @royjr made their first contribution in "HKG: add KIA_FORTE_2019_NON_SCC fingerprint"
  * @ssysm made their first contribution in "Tesla: remove `TESLA_MODEL_X` from `dashcamOnly`"
* Full Changelog: https://github.com/sunnypilot/sunnypilot/compare/v2025.002.000...v2026.001.000

sunnypilot Version 2025.002.000 (2025-11-06)
========================
* What's Changed (sunnypilot/sunnypilot)
  * models: bump model json to v8 by @Discountchubbs
  * Bug: Model UI Crash Fix by @nayan8teen
  * controlsd: add `CP_SP` to `get_pid_accel_limits` by @THERoenPR
  * sunnylink: update uploader button logic to support novice tier and above by @devtekve
  * Tesla: Coop Steering by @AmyJeanes
  * ui: update discord references and add forum widget by @devtekve
  * ui: Fix spacing in sunnylink panel by @devtekve
  * docs: Update README installation branches and discord links by @mpurnell1 in
  * stats: sunnylink integration by @devtekve
  * bug: Fix initial registration for sunnylink by @devtekve
* What's Changed (sunnypilot/opendbc)
  * Honda: add brake hold messages for Clarity by @mvl-boston
  * interface: add `CP_SP` to `get_pid_accel_limits` method signature by @roenthomas
  * Honda: use fixed accel min/max constants for Gas Interceptor by @roenthomas
  * Tesla: Coop Steering by @AmyJeanes
* New Contributors (sunnypilot/sunnypilot)
  * @THERoenPR made their first contribution in "controlsd: add `CP_SP` to `get_pid_accel_limits`"
  * @AmyJeanes made their first contribution in "Tesla: Coop Steering"
  * @mpurnell1 made their first contribution in "docs: Update README installation branches and discord links"
* Full Changelog: https://github.com/sunnypilot/sunnypilot/compare/v2025.001.000...v2025.002.000

sunnypilot Version 2025.001.000 (2025-10-25)
========================
* 🛠️ Major rewrite
  * Most features are intended to be identical to previous versions with slight improvements
  * Fully adopts upstream commaai’s openpilot, opendbc (car interface and safety), and panda test suites to ensure consistent safety compliance and reliability across all systems
  * Added regression testing to verify expected behavior and maintain stability across core modules
  * Aligns with comma.ai’s safety policy: preserving driver monitoring, actuation checks, and safety test suite coverage
  * Some features have not yet been reimplemented in this rewrite and are temporarily disabled in this release. They may return in future releases once fully ported and validated. See the end of the changelog to get a list of what's not going to be present.
* 🌟 Major Features & Systems
  * Modular Assistive Driving System (MADS)
    * Complete driving assistance framework
  * Driving Model Manager
    * Custom driving model selection with support for about 86 models (as of writing), from Night Strike (October 2023) up to The Cool People’s Models (October 2025)
  * Neural Network Lateral Control (NNLC) (Formerly NNFF)
    * Advanced torque-based lateral control
  * Dynamic Experimental Control (DEC)
    * Intelligent longitudinal control adaptation
  * Speed Limit Assist (SLA)
    * Comprehensive speed limit integration featuring @pfeiferj's `mapd` for offline map limits downloads, a Speed Limit Resolver for sourcing data (from car, map, combined, etc), on-screen UI for Speed Limit Information/Warning, and Speed Limit Assist (SLA) to adjust cruise speed automatically.
    * Currently disabled for Tesla with sunnypilot Longitudinal Control in release and Rivian with sunnypilot Longitudinal Control in all branches
      * May return in future releases
  * Intelligent Cruise Button Management (ICBM)
    * System designed to manage the vehicle’s speed by sending cruise control button commands to the car’s ECU.
  * Smart Cruise Control Map & Vision (SCC-M / SCC-V)
    * When using any form of long control (sunnypilot longitudinal control or ICBM) it will control the speed at which you enter and perform a turn by leveraging map data (SCC-M) and/or by leveraging what the model sees about the curve ahead (SCC-V)
  * Vehicle Selector
    * If your vehicle isn’t fingerprinted automatically, you can still use the vehicle selector to get it working
  * sunnylink Integration
    * Cloud connectivity and settings backup/restore
    * PENDING: The infrastructure is ready for remote setting management, including remote driving model switching. An announcement will be made when this is ready to use in current and future releases.
  * External Storage Support
    * Expanded storage options
  * mapd Integration (thanks to @pfeiferj)
    * Allow downloading OpenStreetMap databases for your area, which could be useful for Speed Limit Assist (SLA)
* User Interface Enhancements
  * Complete UI Redesign from Default openpilot Experience
    * A total overhaul of the sunnypilot offroad user interface for a modern and intuitive experience.
  * New Settings Panels
    * Reorganized settings into dedicated panels: Steering, Longitudinal, Vehicle, Models, Visuals, Display, and Trips.
  * Advanced Controls Toggle
    * Out of the box experience has a slightly reduced set of settings for a lower barrier of entry, once you are ready, you can get a few extra settings by toggling on the Advanced Controls.
  * Models Panel
    * A dedicated panel for model management, featuring a download manager, model folders, a favorites system, fuzzy search, and a cache refresh button.
  * Visuals & Display
    * Extensive customization options including brightness controls, custom interactivity timeouts, green light indicator, lead vehicle indicator, on-screen turn signals, blind spot indicators, lead chevron info, standstill timer, road name display, and a Tesla-like 🌈 rainbow road path.
  * Screen Off while driving
    * Options to turn the screen off while driving and customize wake-up behavior for alerts.
  * Branch & Platform Selectors
    * Improved software management with a searchable branch selector and a platform selector that displays the current fingerprint.
  * Developer UI
    * An enhanced developer UI with better alert positioning and an integrated error log viewer.
  * Convenience Features
    * Added an “Exit Offroad” button, “Always Offroad” mode, Quiet Mode, and customizable max time offroad settings.
  * OpenStreetMap Database Downloader
    * The OpenStreetMap database downloader now includes a search feature for easily finding areas.
* Model and AI Improvements
  * Modular Model Backend
    * Major refactor of `modeld` to support modular runners (SNPE, thneed, tinygrad) and dynamic model inputs.
  * Enhanced Model Outputs
    * Models now provide additional outputs like “turn desires” for improved control.
  * Live Parameter Adjustments
    * Support for live delay adjustments and software delay controls directly from the UI.
  * Model Management
    * Added model caching, automatic refresh capabilities, and shape inference from inputs for better compatibility.
* Control Systems
  * Pause Lateral on Blinker
    * Option to temporarily pause lateral control when the turn signal is active.
  * Custom ACC Setpoint Increments
    * Configure custom increments for adjusting the ACC set speed for applicable vehicle platforms.
  * Steering on Brake Press
    * Customizable steering behavior when the brake pedal is pressed.
  * Enforce Torque Lateral Control
    * New customized settings for fine-tuning torque-based steering.
  * Automatic Lane Change
    * Support for automatic lane changes, including a mode to disable it.
* Technical Infrastructure
  * Custom Cereal Implementation
    * Migrated sunnypilot-specific events, car parameters, and car controls to a dedicated cereal for better compatibility and performance.
  * Car Interface Abstractions
    * Refactored car interfaces to support brand-specific settings and easier integration.
  * Param Store Caching
    * Implemented a cache for the parameter store to reduce startup times, with support for live parameter updates.
  * Enhanced Error Handling
    * Improved exception management and Sentry logging for better stability and debugging.
  * Docker & CI/CD
    * Full Docker image support, a dedicated GitHub runner service, and comprehensive improvements to the entire CI/CD pipeline for automated testing, building, and releasing.
* Bug Fixes and Stability
  * Registration Requirement Removed
    * No longer necessary to register the device to go onroad.
  * Panda Firmware Checks
    * Improved firmware checks to gracefully handle deprecated Panda devices.
  * Numerous Fixes
    * Addressed a wide range of bugs across the system for a more stable and reliable experience.
* Developer Experience
  * CLion IDE integration and external tools
  * Comprehensive testing and build automation
  * Model building and publishing automation
  * UI preview generation and testing
  * Release drafting and version management
  * Code quality and maintenance workflows
* Translations and Localization
  * Korean translation updates
  * Automated translation management system
* ❌ Removed
  * Navigate on openpilot (NoO)
    * Navigate on openpilot (NoO) has been removed as upstream is prioritizing improving the driving model’s capabilities and simplifying the training stack.
    * The feature may return in a future upstream release by comma.ai once model improvements from upstream make it more reliable.
  * Visuals: Rocket Fuel
  * Visuals: Displaying Braking Status
  * Vehicle: Toyota - Enforce Stock Longitudinal Control
  * Subaru: Increase Steering Torque
  * Longitudinal: Acceleration Personality
  * UI: Display CPU Temperature on Sidebar
  * Lateral: Block Lane Change with Road Edge Detection
  * UI: Display DM Camera in Reverse Gear
  * UI: Auto-hide Selected UI Elements
  * Visuals: Display End-to-End Longitudinal Status
  * Toyota: Stop and Go Hack (alpha)
  * Visuals: Onroad Settings
  * Honda: Serial Steering Support
  * Volkswagen: Non-ACC Platforms Support
  * Longitudinal: Dynamic Personality
  * Honda Nidec: Allow Stock Longitudinal Control
  * Lateral Planner: Dynamic Lane Profile
  * Lateral Planner: Laneful Mode
  * Lateral: Custom Camera and Path Offsets
  * Toyota: Door Controls
* New Contributors (sunnypilot/sunnypilot)
  * @royjr made their first contribution in "NNLC: bump max similarity for higher accuracy (#704)"
  * @nayan8teen made their first contribution in "UI: Update AbstractControlSP_SELECTOR and OptionControlSP (#800)"
  * @wtogami made their first contribution in "TOYOTA_RAV4_PRIME NNLC tuning gen 1 (#850)"
  * @dparring made their first contribution in "FCA: Ram 1500 improvements (#797)"
  * @Kirito3481 made their first contribution in "Update ko-kr translation (#1167)"
  * @michael-was-taken made their first contribution in "Reorder README tables: show -new branches first (#1191)"
  * @dzid26 made their first contribution in "params: Fix loading delay on startup (#1297)"
  * @HazZelnutz made their first contribution in "Visuals: Turn signals on screen when blinker is used (#1291)"
  * @sirmuskrat made their first contribution in "ui: openpilot Longitudinal Control → sunnypilot Longitudinal Control (#1422)"
* New Contributors (sunnypilot/opendbc)
  * @chrispypatt made their first contribution in "Toyota: SecOC Longitudinal Control (sunnypilot/opendbc#93)"
  * @Discountchubbs made their first contribution in "Hyundai: EPS FW For 2022 KIA_NIRO_EV SCC (sunnypilot/opendbc#118)"
  * @lukasloetkolben made their first contribution in "Tesla: enableBsm is always true (sunnypilot/opendbc#163)"
  * @roenthomas made their first contribution in "Honda: int flag for modified EPS configs (sunnypilot/opendbc#254)"
  * @AmyJeanes made their first contribution in "Tesla: Fix stock LKAS being blocked when MADS is enabled (sunnypilot/opendbc#286)"
  * @mvl-boston made their first contribution in "Honda: Update Clarity brake to renamed DBC message name (sunnypilot/opendbc#282)"
  * @dzid26 made their first contribution in "Tesla: Parse speed limit from CAN (sunnypilot/opendbc#308)"
  * @firestar5683 made their first contribution in "GM: Non-ACC platforms with steering only support (sunnypilot/opendbc#229)"
************************
* Synced with commaai's openpilot (v0.10.1)
  * master commit c9dbf97649a27117be6d5955a49e2d4253337288 (September 12, 2025)
* New driving model
  * World Model: removed global localization inputs
  * World Model: 2x the number of parameters
  * World Model: trained on 4x the number of segments
  * Driving Vision Model: trained on 4x the number of segments
* Honda City 2023 support thanks to vanillagorillaa and drFritz!
* Honda N-Box 2018 support thanks to miettal!
* Honda Odyssey 2021-25 support thanks to csouers and MVL!

sunnypilot - 0.9.7.1 (2024-06-13)
========================
* New driving model
  * Inputs the past curvature for smoother and more accurate lateral control
  * Simplified neural network architecture in the model's last layers
  * Minor fixes to desire augmentation and weight decay
* New driver monitoring model
  * Improved end-to-end bit for phone detection
* Adjust driving personality with the follow distance button
* Support for hybrid variants of supported Ford models
* Fingerprinting without the OBD-II port on all cars
* Improved fuzzy fingerprinting for Ford and Volkswagen
************************
* UPDATED: Synced with commaai's openpilot
  * master commit f8cb04e (June 10, 2024)
* NEW❗: sunnylink (Alpha early access)
  * NEW❗: Config/Settings Backup
    * Remotely back up and restore sunnypilot settings easily
    * Device registration with sunnylink ensures a secure, integrated experience across services
    * AES encryption derived from the device's RSA private key is used for utmost security
    * Settings are encrypted on-device, transmitted securely via HTTPS, and stored encrypted on sunnylink
    * Prevents loss of settings after device resets, offering peace of mind through end-to-end encryption
    * Early alpha access to all current and previous GitHub Sponsors and Patreon supporters
  * GitHub account pairing from device settings scanning QR code
    * Pairing your account will allow you to access features via our API (still WIP but accessible if you dig a little on our code 😉)
    * Allow inheritance of your sponsorship status, allowing you to get extra features and early access whenever applicable
* NEW❗: iOS Siri Shortcuts Navigation support thanks to twilsonco and mike86437!
  * iOS and macOS Shortcuts to quickly set navigation destinations from your iOS device
  * comma Prime support
  * Personal Mapbox/Amap/Google Maps token support
  * Instructions on how to set up your iOS Siri Shortcuts: https://routinehub.co/shortcut/17677/
* NEW❗: Forced Offroad mode
  * Force sunnypilot in the offroad state even when the car is on
  * When Forced Offroad mode is on, allows changing offroad-only settings even when the car is turned on
  * To engage/disengage Force Offroad, go to Settings -> Device panel
* UPDATED: Auto Lane Change Timer -> Auto Lane Change by Blinker
  * NEW❗: New "Off" option to disable lane change by blinker
* UPDATED: Pause Lateral Below Speed with Blinker
  * NEW❗: Customizable Pause Lateral Speed
    * Pause lateral actuation with blinker when traveling below the desired speed selected. Default is 20 MPH or 32 km/h.
* UPDATED: Hyundai CAN Longitudinal
  * Auto-enable radar tracks on platforms with applicable Mando radar
* UPDATED: Hyundai CAN-FD Camera-based SCC
  * NEW❗: Parse lead info for camera-based SCC platforms with longitudinal support
    * Improve lead tracking when using openpilot longitudinal
* RE-ENABLED: Map-based Turn Speed Control (M-TSC) for supported platforms
  * openpilot Longitudinal Control available cars
  * Custom Stock Longitudinal Control available cars
* UPDATED: Continued support for comma Pedal
  * In response to the official deprecation of support for comma Pedal in the upstream, sunnypilot will continue maintaining software support for comma Pedal
* UPDATED: Driving Model Selector v4
  * NEW❗: Driving Model additions
    * North Dakota (April 29, 2024) - NDv2
    * WD40 (April 09, 2024) - WD40
    * Duck Amigo (March 18, 2024) - DA
    * Recertified Herbalist (March 01, 2024) - CHLR
  * Legacy Driving Models with Navigate on openpilot (NoO) support
    * Includes Duck Amigo and all preceding models
* UPDATED: Bumping mapd by [@pfeiferj](https://github.com/pfeiferj) to version [v1.9.0](https://github.com/pfeiferj/mapd/releases/tag/v1.9.0) thanks to pfeiferj!
* UPDATED: Reset Mapbox Access Token -> Reset Access Tokens for Map Services
  * Reset self-service access tokens for Mapbox, Amap, and Google Maps
* UPDATED: Upstream native support for Gap Adjust Cruise
* UPDATED: Neural Network Lateral Control (NNLC)
  * Due to upstream changes with platform simplifications, most platforms will match and fallback to combined platform model
  * This will be updated when the new mapping of platforms are restructured (thanks @twilsonco 😉)
* UI Updates
  * Display Metrics Below Chevron
    * NEW❗: Metrics is now being displayed below the chevron instead of above
    * NEW❗: Display both Distance and Speed simultaneously
    * NEW❗: View sunnylink connectivity status on the left sidebar!

sunnypilot - 0.9.6.2 (2024-05-29)
========================
* REMOVED: Screen Recorder
  * Screen Recorder is removed due to unnecessary resource usage
  * An improved version will be available in the near future. Stay tuned!

sunnypilot - 0.9.6.1 (2024-02-27)
========================
* New driving model
  * Vision model trained on more data
  * Improved driving performance
  * Directly outputs curvature for lateral control
* New driver monitoring model
  * Trained on larger dataset
* AGNOS 9
* comma body streaming and controls over WebRTC
* Improved fuzzy fingerprinting for many makes and models
* Alpha longitudinal support for new Toyota models
* Chevrolet Equinox 2019-22 support thanks to JasonJShuler and nworb-cire!
* Dodge Durango 2020-21 support
* Hyundai Staria 2023 support thanks to sunnyhaibin!
* Kia Niro Plug-in Hybrid 2022 support thanks to sunnyhaibin!
* Lexus LC 2024 support thanks to nelsonjchen!
* Toyota RAV4 2023-24 support
* Toyota RAV4 Hybrid 2023-24 support
************************
* UPDATED: Synced with commaai's openpilot
  * master commit db57a21 (February 22, 2024)
  * v0.9.6 release (February 27, 2024)
* UPDATED: Dynamic Experimental Control (DEC)
  * Synced with dragonpilot-community/dragonpilot:beta3 commit f4ee52f
* NEW❗: Default Driving Model: Certified Herbalist v2 (February 13, 2024)
* UPDATED: Driving Model Selector v3
  * NEW❗: Driving Model additions
    * Certified Herbalist v2 (February 13, 2024) - CHv2
    * Certified Herbalist (February 5, 2024) - CH
    * Los Angeles v2 (January 24, 2024) - LAv2
    * Los Angeles (January 22, 2024) - LAv1
  * NEW❗: Model Caching thanks to DevTekVE!
    * Model caching allows the selection of previously downloaded Driving Model
    * Users can now access cached versions of selected models, eliminating redundant downloads for previously fetched models
  * Legacy Driving Models support
    * New Delhi (December 21, 2023) - ND
    * Blue Diamond v2 (December 11, 2023) - BDv2
    * Blue Diamond (November 18, 2023) - BDv1
    * Farmville (November 7, 2023) - FV
    * Night Strike (October 3, 2023) - NS
  * Certain features are deprecated with newer Driving Models
    * Dynamic Lane Profile (DLP)
    * Custom Offsets
* UPDATED: Dynamic Lane Profile (DLP)
  * Continued support for Legacy Driving Models (e.g., ND, BDv2, BDv1, FV, NS)
  * Deprecated support for newer Driving Models (e.g., CHv2, CH, LAv2, LAv1)
* UPDATED: Custom Offsets
  * Continued support for Legacy Driving Models (e.g., ND, BDv2, BDv1, FV, NS)
  * Deprecated support for newer Driving Models (e.g., CHv2, CH, LAv2, LAv1)
* UPDATED: Hyundai/Kia/Genesis - ESCC Radar Interceptor
  * Message parsing improvements with the latest firmware update: https://github.com/sunnypilot/panda/tree/test-escc-smdps
* UI Updates
  * NEW❗: Visuals: Display Feature Status toggle
    * Display the statuses of certain features on the driving screen
  * NEW❗: Visuals: Enable Onroad Settings toggle
    * Display the Onroad Settings button on the driving screen to adjust feature options on the driving screen, without navigating into the settings menu
  * REMOVED: "Device ambient" temperature option on the sidebar
* FIXED: New comma 3X support
* FIXED: New comma eSIM support
* Bug fixes and performance improvements

sunnypilot - 0.9.5.3 (2023-12-24)
========================
* UPDATED: Dynamic Experimental Control (DEC)
  * Synced with dragonpilot-community/dragonpilot:lp-dp-beta2 commit 578d38b
* UPDATED: Driving Model Selector v2
  * Driving models sort in descending order based on availability date
  * Experimental/unmerged driving models are only available in "dev-c3" branch
    * To select and use experimental driving models, navigate to "Software" panel, select the "dev-c3" branch, and check for update
* UPDATED: Vision-based Turn Speed Control (V-TSC) implementation
  * Refactored implementation thanks to pfeiferj!
  * More accurate and consistent velocity calculation to achieve smoother longitudinal control in curves
* NEW❗: Speed Limit Warning
  * Display alert and/or chime to warn the driver when the cruising speed is faster than the speed limit plus the Warning Offset
  * Customizable Warning Offset, independent of Speed Limit Control (SLC)'s Limit Offset
* UPDATED: Speed Limit Source Policy
  * Selectable speed limit source for Speed Limit Control and Speed Limit Warning
  * Applicable to: Speed Limit Control, Speed Limit Warning
* UPDATED: Speed Limit Control (SLC)
  * Engage Mode: Removed "Warning Only" mode - this has been replaced by the new Speed Limit Warning sub-menu
* UPDATED: OpenStreetMap (OSM) implementation
  * Refactored implementation thanks to pfeiferj!
    * Less resource impact
    * Significantly smaller sizes with databases
    * All regions are available to download
    * Weekly map updates thanks to pfeiferj!
    * Increased the font size of the road name
  * C3X-specific changes
    * Altitude (ALT.) display on Developer UI
    * Current street name on top of driving screen when "OSM Debug UI" is enabled
* UPDATED: Map-based Turn Speed Control (M-TSC) implementation
  * Only available in "staging-c3" and "dev-c3" branches. If you are using "release-c3" branch, navigate to "Software" panel, select the desired target branch, and check for update
  * Refactored implementation thanks to pfeiferj!
  * Based on the new OpenStreetMap implementation
  * Improved predicted curvature calculations from OpenStreetMap data
* UI updates
  * RE-ENABLED: Navigation: Full screen support
    * Display the map view in full screen
    * To switch back to driving view, tap on the border edge
* Hyundai Bayon Non-SCC 2019 support thanks to polein78!

sunnypilot - 0.9.5.2 (2023-12-07)
========================
* NEW❗: MADS: Allow Navigate on openpilot in Chill Mode
  * Allow navigation to feed map view into the driving model while using Chill Mode
  * Support all platforms, including platforms that do not support openpilot longitudinal control & Experimental Mode
* NEW❗: Neural Network Lateral Controller
  * Formerly known as "NNFF", this replaces the lateral "torque" controller with one using a neural network trained on each car's (actually, each separate EPS firmware) driving data for increased controls accuracy
  * Contact @twilsonco in the sunnypilot Discord server with feedback, or to provide log data for your car if your car is currently unsupported
* NEW❗: Driving Model Selector
  * Easily switch between driving models without reinstalling branches. Offering immediate access to the latest models upon release
    * An internet connection is required for downloading models. Each model switch currently involves downloading the model again. Future updates may allow for offline switching
  * Warning is displayed for metered connections to avoid unexpected data usage if on cellular data
  * Change driving models via **Settings -> Software -> Current Driving Model**.
* NEW❗: Hyundai CAN longitudinal:
  * NEW❗: Enable radar tracks for certain Santa Fe platforms
    * Internal Combustion Engine (ICE) 2021-23
    * Hybrid 2022-23
    * Plug-in Hybrid 2022-23
* NEW❗: Lane Change: When manually braking with steering engaged, turning on the turn signal will default to Nudge mode
* Volkswagen MQB CC only platforms (radar or no radar) support thanks to jyoung8607!

sunnypilot - 0.9.5.1 (2023-11-17)
========================
* UPDATED: Synced with commaai's master commit e94c3c5
* NEW❗: Farmville driving model
* NEW❗: Onroad Settings Panel
  * Onroad buttons (i.e., DLP, GAC) moved to its dedicated panel
    * Driving Personality
    * Dynamic Lane Profile (DLP)
    * Dynamic Experimental Control (DEC)
    * Speed Limit Control (SLC)
* NEW❗: Display main feature status on onroad view in real-time
  * GAP - Driving Personality
  * DLP - Dynamic Lane Profile
  * DEC - Dynamic Experimental Control
  * SLC - Speed Limit Control
* NEW❗: Dynamic Experimental Control (DEC) thanks to dragonpilot-community!
  * Automatically determines and selects between openpilot ACC and openpilot End to End longitudinal based on conditions for a more natural drive
  * Dynamic Experimental Control is only active while in Experimental Mode
  * When Dynamic Experimental Control is ON, initially setting cruise speed will set to the vehicle's current speed
* NEW❗: Hyundai CAN longitudinal:
  * NEW❗: Parse lead info for camera-based SCC platforms
    * Improve lead tracking when using openpilot longitudinal
  * NEW❗: Parse lead distance to display on car cluster
    * Introduced better lead distance calculation to display on the car's cluster, replacing the binary "lead visible" indication on the SCC cluster
    * Lead distance is now categorized into different ranges for more detailed and comprehensive information to the driver similar to how stock ACC does it
  * NEW❗: Parse speed limit sign recognition from camera for certain supported platforms
* NEW❗: Subaru - Stop and Go auto-resume support thanks to martinl!
  * Global (excluding Gen 2 and Hybrid) and Pre-Global support
* NEW❗: Toyota - Stop and Go hack
  * Allow some Toyota/Lexus cars to auto resume during stop and go traffic
  * Only applicable to certain models and model years
* NEW❗: Toyota: ZSS support thanks to dragonpilot-community and ErichMoraga!
* NEW❗: MSPA (Cereal structs refactor)
  * Make sunnypilot Parsable Again - @sshane
  * sunnypilot is now parsable with stock openpilot tools
* NEW❗: Display 3D buildings on map thanks to jakethesnake420!
* openpilot Longitudianl Control capable cars only
  * UPDATED: Gap Adjust Cruise is now a part of Driving Personality
    * [DISTANCE/FOLLOW DISTANCE/GAP DISTANCE] physical button on the steering wheel to select Driving Personality on by default
    * Status now viewable in onroad view or Onroad Settings Panel
    * REMOVED: Gap Adjust Cruise toggle
* UPDATED: Speed Limit Control (SLC)
  * NEW❗: Speed Limit Engage Mode
    * Select the desired mode to set the cruising speed to the speed limit
      * Warning Only: Warn the driver when the vehicle is driven faster than the speed limit
      * Auto: Automatic speed adjustment on motorways based on speed limit data
      * User Confirm: Inform the driver to change set speed of Adaptive Cruise Control to help the driver stay within the speed limit
    * Supported platforms
      * openpilot Longitudinal Control available cars (Excluding certain Toyota/Lexus, Ford, explained below)
      * Custom Stock Longitudinal Control available cars
    * Unsupported platforms
      * Toyota/Lexus and Ford - most platforms do not allow us to control the PCM's set speed, requires testers to verify
  * NEW❗: Speed limit source selector
    * Select the desired precedence order of sources used to adapt cruise speed to road limits
* UPDATED: Custom Stock Longitudinal Control
  * RE-ENABLED: Hyundai/Kia/Genesis CAN-FD platforms
* UPDATED: Custom Offsets reimplementation
  * Camera Offset only works in Laneful (Laneful Only or Laneful in Auto mode when using Dynamic Lane Profile)
  * Path Offset can be applied to both Laneless and Laneful
* UPDATED: Refactored Torque Lateral Control custom tuning menu
  * NEW❗: Less Restrict Settings for Self-Tune (Beta)
  * NEW❗: Custom Tuning for setting offline and live values in real-time
* UPDATED: Auto-detect custom Mapbox token if a personal Mapbox token is provided
  * REMOVED: "Enable Mapbox Navigation" toggle
* UI updates
  * New Settings menu redesign and improved interactions
* FIXED: Retain hotspot/tethering state was not consistently saved
* FIXED: Map stuck in "Map Loading" if comma Prime is active
* FIXED: OpenStreetMap implementation on C3X devices
  * M-TSC
  * Altitude (ALT.) display on Developer UI
  * Current street name on top of driving screen when "OSM Debug UI" is enabled
* Hyundai Kona Non-SCC 2019 support thanks to Quex!
* Kia Seltos Non-SCC 2023-24 support thanks to Moodkiller and jeroid_!

sunnypilot - 0.9.4.1 (2023-08-11)
========================
* UPDATED: Synced with commaai's 0.9.4 release
* NEW❗: Moonrise driving model
* NEW❗: Ford upstream models support
* UPDATED: Dynamic Lane Profile selector in the "SP - Controls" menu
* REMOVED: Dynamic Lane Profile driving screen UI button
* FIXED: Disallow torque lateral control for angle control platforms (e.g. Ford, Nissan, Tesla)
  * Torque lateral control cannot be used by angle control platforms, and would cause a "Controls Unresponsive" error if Torque lateral control is enforced in settings
* REMOVED: Speed Limit Style override
* Honda Accord 2016-17 support thanks to mlocoteta!
  * Serial Steering hardware required. For more information, see https://github.com/mlocoteta/serialSteeringHardware
* mapd: utilize advisory speed limit in curves (#142) thanks to pfeiferj!

sunnypilot - 0.9.3.1 (2023-07-09)
========================
* UPDATED: Synced with commaai's 0.9.3 release
* NEW❗: Display Temperature on Sidebar toggle
  * Display Ambient temperature, memory temperature, CPU core with the highest temperature, GPU temperature, or max of Memory/CPU/GPU on the sidebar
  * Replace "Display CPU Temperature on Sidebar" toggle
* NEW❗: Hot Coffee driving model
* NEW❗: HKG CAN: Smoother Stopping Performance (Beta) toggle
  * Smoother stopping behind a stopped car or desired stopping event.
  * This is only applicable to HKG CAN platforms using openpilot longitudinal control
* NEW❗: Toyota: TSS2 longitudinal: Custom Tuning
  * Smoother longitudinal performance for Toyota/Lexus TSS2/LSS2 cars thanks to dragonpilot-community!
* NEW❗: Enable Screen Recorder toggle
  * Enable this will display a button on the onroad screen to toggle on or off real-time screen recording with UI elements.
* IMPROVED: Dynamic Lane Profile: when using Laneline planner via Laneline Mode or Auto Mode, enforce Laneless planner while traveling below 10 MPH or 16 km/h
* REMOVED: Display CPU Temperature on Sidebar

sunnypilot - 0.9.2.3 (2023-06-18)
========================
* NEW❗: Auto Lane Change: Delay with Blind Spot
  * Toggle to enable a delay timer for seamless lane changes when blind spot monitoring (BSM) detects an obstructing vehicle, ensuring safe maneuvering
* NEW❗: Driving Screen Off: Wake with Non-Critical Events
  * When Driving Screen Off Timer is not set to "Always On":
    * Enabled: Wake the brightness of the screen to display all events
    * Disabled: Wake the brightness of the screen to display critical events
  * Currently, all non-nudge modes are default to continue lane change after 1 seconds of blind spot detection
* NEW❗: Fleet Manager PIN Requirement toggle
  * User can now enable or disable PIN requirement on the comma device before accessing Fleet Manager
* NEW❗: Reset all sunnypilot settings toggle
* NEW❗: Turn signals display on screen when blinker is used
  * Green: Blinker is on
  * Red: Blinker is on, car detected in the adjacent blind spot or road edge detected
* IMPROVED: mapd: better exceptions handling when loading dependencies
* UPDATED: Green Traffic Light Chime no longer displays an orange border when executed
* FIXED: mapd: Road name flashing caused by desync with last GPS timestamp
* FIXED: Ram HD (2500/3500): Ignore paramsd sanity check
  * Live parameters have trouble with self-tuning on this platform with upstream openpilot 0.9.2
* Hyundai: Longitudinal support for CAN-based Camera SCC cars thanks to Zack1010OP's Patreon sponsor!

sunnypilot - 0.9.2.2 (2023-06-13)
========================
* NEW❗: Toyota: Allow M.A.D.S. toggling with LKAS Button (Beta)
* IMPROVED: Ram: cruise button handling

sunnypilot - 0.9.2.1 (2023-06-10)
========================
* UPDATED: Synced with commaai's 0.9.2 release
* UPDATED: feature revamp with better stability
* UPDATED:
  * M.A.D.S.
    * Path color becomes LIGHT ORANGE during Driver Steering Override
  * Gap Adjust Cruise (now known as Driving Personality in upstream openpilot 0.9.3):
    * Updated profiles and jerk changes
    * Experimental Mode support
    * Three settings: Stock, Aggressive, and Maniac
    * Stock is recommended and the default
    * In Aggressive/Maniac mode, lead follow distance is shorter and quicker gas/brake response
  * Dynamic Lane Profile
    * Display blue borders on both sides of the driving path when Laneline mode is being used in the planner
    * Auto Mode optimization
      * Permanent: Laneless during Auto Lane Change execution
  * Mapd
    * OpenStreetMap Database: new regions added
  * Developer UI (Dev UI)
    * REMOVED: 2-column design
    * NEW❗: 1-column + 1-row design
  * Custom Stock Longitudinal Control
    * NEW❗: Chrysler/Jeep/Ram support
    * NEW❗: Mazda support
    * NEW❗: Volkswagen PQ support
    * DISABLED: Hyundai/Kia/Genesis CAN-FD platforms
* NEW❗: Switch between Chill (openpilot ACC) and Experimental (E2E longitudinal) with DISTANCE button on the steering wheel
  * To switch between Chill and Experimental Mode: press and hold the DISTANCE button on the steering wheel for over 0.5 second
  * All openpilot longitudinal capable cars support
* NEW❗: Nicki Minaj driving model
* NEW❗: Nissan and Mazda upstream models support
* NEW❗: Pre-Global Subaru upstream models support
* NEW❗: Display End-to-end Longitudinal Status (Beta)
  * Display an icon that appears when the End-to-end model decides to start or stop
* NEW❗: Green Traffic Light Chime (Beta)
  * A chime will play when the traffic light you are waiting for turns green, and you have no vehicle in front of you.
* NEW❗: Lead Vehicle Departure Alert
  * Notify when the leading vehicle drives away
* NEW❗: Speedometer: Display True Speed
  * Display the true vehicle current speed from wheel speed sensors.
* NEW❗: Speedometer: Hide from Onroad Screen
* NEW❗: Auto-Hide UI Buttons
  * Hide UI buttons on driving screen after a 30-second timeout. Tap on the screen at anytime to reveal the UI buttons
  * Applicable to Dynamic Lane Profile (DLP) and Gap Adjust Cruise (GAC)
* NEW❗: Display DM Camera in Reverse Gear
  * Show Driver Monitoring camera while the car is in reverse gear
* NEW❗: Block Lane Change: Road Edge Detection (Beta)
  * Block lane change when road edge is detected on the stalk actuated side
* NEW❗: Display CPU Temperature on Sidebar
  * Display the CPU core with the highest temperature on the sidebar
* NEW❗: Display current driving model in Software settings
* NEW❗: HKG: smartMDPS automatic detection (installed with applicable firmware)
* FIXED: Unintended siren/alarm from the comma device if the vehicle is turned off too quickly in PARK gear
* FIXED: mapd: Exception handling for loading dependencies
* Fleet Manager via Browser support thanks to actuallylemoncurd, AlexandreSato, ntegan1, and royjr!
  * Access your dashcam footage, screen recordings, and error logs when the car is turned off
  * Connect to the device via Wi-Fi, mobile hotspot, or tethering on the comma device, then navigate to http://ipAddress:5050 to access.
* Honda Clarity 2018-22 support thanks to mcallbosco, vanillagorillaa and wirelessnet2!
* Ram: Steer to 0/7 MPH support thanks to vincentw56!
* Retain hotspot/tethering state across reboots thanks to rogerioaguas!

sunnypilot - Version Latest (2023-02-22)
========================
* UPDATED: Synced with commaai's master branch - 2023.02.19-04:52:00:GMT - 0.9.2
* Refactor sunnypilot features to be more stable

sunnypilot - Version Latest (2022-12-16)
========================
* UPDATED: Synced with commaai's master branch - 2022.12.16-06:31:00:GMT - 0.9.1
* NEW❗: GM:
    * NEW❗: Gap Adjust Cruise support - Chill, Normal, Aggressive
    * NEW❗: Experimental Mode: Hold DISTANCE button on the steering wheel for 0.5 second to switch between Experimental Mode and Chill Mode
* REMOVED❌: Toytoa: SnG Hack
    * This method is not recommended and may cause some cars to not behave as expected
    * SDSU is strongly recommended to enable SnG for Toyota vehicles without SnG from factory
* commaai: radard: add missing accel data for vision-only leads (commaai/openpilot#26619) - pending PR
    * VOACC performance is drastically improved when using Chill Mode
* IMPROVED: M.A.D.S. events handling
* IMPROVED: UI: screen recorder button change
* IMPROVED: OpenStreetMap Offline Database optimization
* FIXED: Toyota: vehicles' LKAS button no longer has a delay with toggling M.A.D.S.
* FIXED: Toyota: brake pedal press at standstill causing Cruise Fault
* FIXED: Volkswagen MQB: reduce Camera Malfunction occurrences (requires testing)
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-12-10)
========================
* IMPROVED: NEW❗ Developer UI design
    * Second column metrics is now moved to the bottom of the screen
        * ACC. = Acceleration
        * L.S. = Lead Speed
        * E.T. = EPS Torque
        * B.D. = Bearing Degree
        * FRI. = Friction
        * L.A. = Lateral Acceleration
        * ALT. = Altitude
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-12-07)
========================
* NEW❗: Screen Recorder support thanks to neokii and Kumar!
* NEW❗: End-to-end longitudinal start/stop status icon
    * Only appears when Experimental Mode is enabled
* NEW❗: End-to-end longitudinal car chime when starting
    * Hyundai/Kia/Genesis CAN platform, Honda/Acura Bosch/Nidec, Toyota/Lexus
    * i.e. Traffic light turns green, stop sign ready to go, etc.
    * Only appears when Experimental Mode is enabled AND longitudinal control is disengaged
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-12-05)
========================
* UPDATED: Synced with commaai's master branch - 2022.12.04-22:46:00:GMT - 0.9.1
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-11-12)
========================
* UPDATED: Synced with commaai's master branch - 2022.11.12-10:02:00:GMT - 0.8.17
* FIXED: CAN Error for CAN HKG cars that do not have navigation from the factory
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-11-11)
========================
* UPDATED: Synced with commaai's master branch - 2022.11.11-21:22:00:GMT - 0.8.17
* commaai: AGNOS 6.2 (commaai/openpilot#26441)
* NEW❗: Speed Limit Control - HKG - add speed limit from car's navigation head unit
    * Compatible with certain models, trims, and model years
* DISABLED: FCA: RAM HD - steer down to 0
* FIXED: UI: End-to-end longitudinal button on driving screen synchronization
* FIXED: Honda: Longitudinal status with set cruise speed now displays properly in the car's dashboard
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-11-08)
========================
* ADDED: New Zealand offline OpenStreetMap database

sunnypilot - Version Latest (2022-11-04)
========================
* UPDATED: Synced with commaai's master branch - 2022.11.05-01:44:00:GMT - 0.8.17
* RE-ENABLED: Dynamic Lane Profile - preserves lanelines
    * Can be found in "SP - Controls" menu
* NEW❗: DLP: switch to laneless for current/future curves thanks to @twilsonco!
    * Can be found in "SP - Controls" menu
* NEW❗: UI: Road Camera Selector
    * Enable this will display a button on the driving screen to select the driving camera
    * Can be found in "SP - Visuals" menu
* NEW❗: Controls: Camera & Path Custom Offsets
    * Only applicable to laneline mode when using Dynamic Lane Profile
* NEW❗: Buttons on driving screen are now sorted based on priority and availability
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-28)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.28-03:53:00:GMT - 0.8.17
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-26)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.26-06:20:00:GMT - 0.8.17
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-25)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.25-23:53:00:GMT - 0.8.17
* Pre-Global Subaru support thanks to @martinl!
* NEW❗: Speed Limit values turn red when current speed is higher than posted speed limit
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-23)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.22-23:15:00:GMT - 0.8.17
* IMPROVED: Custom Stock Longitudinal Control - HKG - only allow engagement on user button press
* IMPROVED: Custom Stock Longitudinal Control - Volkswagen MQB & PQ - more consistent set speed change
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-21)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.21-17:33:00:GMT - 0.8.17
* IMPROVED: Custom Stock Longitudinal Control - Volkswagen MQB & PQ - more predictable button send logic
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-20)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.20-20:25:00:GMT - 0.8.17
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-19)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.19-08:31:00:GMT - 0.8.17
* IMPROVED: Controls: Speed Limit Control - accelerator press only disengage if "Disengage on Accelerator Pedal" is enabled
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-18)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.18-04:44:00:GMT - 0.8.17
* RE-ENABLED: Volkswagen MQB & PQ with Custom Stock Longitudinal Control
* NEW❗: Steering Rate Cost Live Tune
    * Enables live tune for Steering Rate Cost. Lower value allows steering wheel to move more freely at low speed
    * Can be found in "SP - Controls" menu
* FIXED: MADS: GM - include Regen Paddle logic thanks to @twilsonco!
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-17)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.17-23:54:00:GMT+1 - 0.8.17
* ENABLED: "Custom Stock Longitudinal Control" toggle for CAN-FD cars
* FIXED: HKG CAN-FD: Could not engage when openpilot longitudinal is enabled
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-13)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.13-19:43:00:GMT+1 - 0.8.17
* ADDED: Live Tmux toggle
    * Can be found in "SP - General" menu
* IMPROVED: OpenStreetMap Database Update - only check for database update with explicit user decision
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-11)
========================
* ADDED: Hyundai openpilot longitudinal improvements - huge thanks to @aragon7777!
* ADDED: Check for OpenStreetMap Database Update button
* UPDATED: commaai: Low speed lateral control improvements (commaai:openpilot#26022, bbcd448) - pending PR
* FIXED: MUTCD speed limit spacing adjusts dynamically when no subtext is shown (i.e., speed limit offset, distance to next speed limit)
* FIXED: MADS: Intermittent CAN Error when engaging for Toyota Prius TSS-P
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-09)
========================
* ADDED: commaai: Low speed lateral control improvements (commaai:openpilot#26022, bca288bb) - pending PR
* FIXED: MADS: Intermittent CAN Error when engaging for Toyota Prius TSS-P
* IMPROVED: mapd: stop signs and other supported traffic_calming tags are now slowing/stopping as expected
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-08)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.08-12:07:00:GMT+1 - 0.8.17
* FIXED: MADS: Intermittent CAN Error when engaging for Toyota Prius TSS-P
* IMPROVED: mapd: Speed Humps are now set at 20 MPH or 32 km/h
* IMPROVED: OpenStreetMap Offline Database download experience
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-10-07)
========================
* UPDATED: Synced with commaai's master branch - 2022.10.07-08:16:00:GMT - 0.8.17
* NEW❗: OpenStreetMap database can now be downloaded locally for offline use
    * Now offering US South, US West, US Northeast, US Florida, Taiwan, and South Africa
    * Databases updated - 2022.10.05-03:30:00:GMT
* NEW❗: mapd: Stop Sign, Yield, Speed Bump, Speed Hump, Sharp Curve support - huge thanks to @move-fast and @dragonpilot-community!
    * Go to https://openstreetmap.org and start mapping out your area!
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-09-30)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.30-22:43:00:GMT - 0.8.17
* RE-ADDED: Torque Lateral Controller Live Tune Menu
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-09-23)
========================
* ADDED: Developer UI: latAccelFactorFiltered & frictionCoefficientFiltered values displays in green if Torque is using live params
* Bug fixes and performance improvements

sunnypilot - Version Latest (2022-09-22)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.19-22:19:00:GMT - 0.8.17
* NEW❗: Toggle to explicitly enable Custom Stock Longitudinal Control
    * Applicable cars only: Honda, Hyundai/Kia/Genesis
    * Settings -> Toggles menu

sunnypilot - Version Latest (2022-09-21)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.19-22:19:00:GMT - 0.8.17
* ADDED: Toggle to enable Live Torque (self/auto tune) with Torque lateral controller
    * To enable, first enable "Enforce Torque Lateral Controller" toggle
* UPDATED: New metrics in Developer UI (when Live Torque is enabled)
    * REMOVED: latAccelFactorRaw & frictionCoefficientRaw from torqued
    * ADDED: latAccelFactorFiltered & frictionCoefficientFiltered from torqued
* REMOVED: Temporary remove Torque Lateral Controller Live Tune Menu

sunnypilot - Version Latest (2022-09-20)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.19-22:19:00:GMT - 0.8.17
* ADDED: Toggle to enable Live Torque (self/auto tune) with Torque lateral controller
    * To enable, first enable "Enforce Torque Lateral Controller" toggle
* REMOVED: Temporary remove Torque Lateral Controller Live Tune Menu

sunnypilot - Version Latest (2022-09-18)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.17-11:23:00:GMT - 0.8.17
* ADDED: Kia Forte Non-SCC 2019 support for @askalice
* FIXED: Torque Lateral Control Live Tune now syncs with commaai:openpilot#25822
* FIXED: mapd dependencies no longer need to be re-downloaded after unknown reboots

sunnypilot - Version Latest (2022-09-17)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.17-11:23:00:GMT - 0.8.17
* NEW❗: Non SCC HKG support
    * Custom Stock Longitudinal Control
    * ❗No❗ openpilot longitudinal control
* FIXED: Honda Bosch random low-value set speed changes

sunnypilot - Version Latest (2022-09-16)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.16-20:23:00:GMT - 0.8.17

sunnypilot - Version Latest (2022-09-15)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.16-02:00:00:GMT - 0.8.17
* FIXED: Block additional auto lane change actions if blinker stays on after the first lane change
* REVERTED: Some Toyota with LKAS button no longer requires double press to engage/disengage M.A.D.S.

sunnypilot - Version Latest (2022-09-14)u
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: GM models supported in Force Car Recognition (FCR)
    * Under "SP - Vehicles"
* NEW❗: Prompt to select car in "SP - Vehicles" if car unrecognized on startup
* FIXED: Some Toyota with LKAS button no longer requires double press to engage/disengage M.A.D.S.
* UPDATED: ESCC: Use radar tracks from radar if available

sunnypilot - Version Latest (2022-09-13)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: New metric in Developer UI
    * Actual Lateral Acceleration (Roll Compensated)

sunnypilot - Version Latest (2022-09-12)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* FIXED: Honda Nidec models not gaining speed when longitudinal engaged

sunnypilot - Version Latest (2022-09-11)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: Hyundai Enhanced SCC now forwards FCW and AEB signals and commands from radar to car
* RE-ENABLED: MADS Status Icon toggle

sunnypilot - Version Latest (2022-09-10)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.11-02:47:00:GMT - 0.8.17
* NEW❗: RAM improvement implementation thanks to realfast!
* DISABLED: Chrysler/Jeep/Ram with Custom Stock Longitudinal Control
* DISABLED: Volkswagen MQB & PQ with Custom Stock Longitudinal Control

sunnypilot - Version Latest (2022-09-09)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.09-07:35:00:GMT - 0.8.17
* NEW❗: MADS now supporting General Motors (GM)
* ADDED: Custom Stock Longitudinal Control - Volkswagen
    * MQB & PQ
* ADDED: Reverse ACC Change
    * ACC +/-: Short=5, Long=1
* ADDED: Custom Stock Longitudinal Control
    * Hyundai/Kia/Genesis
    * Honda Bosch
* ADDED: Hyundai: 2015-16 Genesis resume from standstill fix (commaai:openpilot#25579) - pending PR
* Vision Turn Speed Control re-enabled
* Disable Onroad Uploads toggle re-enabled

sunnypilot - Version Latest (2022-09-08)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.08-04:05:00:GMT - 0.8.17
* NEW❗: Block lane change initiation while brake is pressed

sunnypilot - Version Latest (2022-09-07)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.08-04:05:00:GMT - 0.8.17
* NEW❗: Display End-to-end longitudinal 🌮 on screen
    * NEW❗: Hold DISTANCE button on the steering wheel for 1 second to switch between E2E Long and ACC mode
    * Enable toggle on the driving screen to switch between modes with End-to-end longitudinal
    * Only applicable to cars with openpilot longitudinal control
* NEW❗: Block lane change initiation while brake is pressed
* REMOVED: Dynamic Lane Profile - upstream laneless model is now on by default
* REMOVED: hyundai: consistent start from stop (commaai:openpilot#25672) - pending PR

sunnypilot - Version Latest (2022-09-06)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.06 - 0.8.17
* NEW❗: Display useful metrics above the chevron that tracks the lead car
    * Under "SP - Visuals" menu
    * Only applicable to cars with openpilot longitudinal control
* ADDED: hyundai: consistent start from stop (commaai:openpilot#25672) - pending PR
* FIXED: Vienna speed limit interface now scales properly with the outer box
* REMOVED: Hyundai long improvements (commaai:openpilot#25604) - closed PR

sunnypilot - Version Latest (2022-09-05)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.03 - 0.8.17
* NEW❗: Speed Limit Control (SLC) interface integrated with upstream
* NEW❗: Speed limit from active navigation is now prioritized for Speed Limit Control
* NEW❗: MUTCD (U.S.) or Vienna (E.U.) speed limit interfaces can now be selected under "SP - Controls"

sunnypilot - Version Latest (2022-09-04)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.03 - 0.8.17
* FIXED: Gap Adjust Cruise status now displays properly on screen
* FIXED: mapd - missing index in list caused mapd to crash
* REMOVED: Temporary removed Vision Turn Speed Control

sunnypilot - Version Latest (2022-09-03)
========================
* UPDATED: Synced with commaai's master branch - 2022.09.03 - 0.8.17
* ADDED: New border colors for different operation engagements
* ADDED: UI: Show barrier when car detected in blind spot
    * Only applicable to cars that have BSM detection with openpilot
* FIXED: Cruise Cancel button no longer display prompt if cruise not engaged
* TWEAKED: Update changelogs on startup in Settings -> Software -> Version
* REMOVED: Upload Raw Logs and Full Resolution Videos toggles

sunnypilot - Version Latest (2022-08-31)
========================
* UPDATED: Synced with commaai's master branch - 2022.08.31 - 0.8.17
* ADDED: New border colors for different operation engagements
* ADDED: UI: Show barrier when car detected in blind spot
    * Only applicable to cars that have BSM detection with openpilot
* FIXED: Cruise Cancel button no longer display prompt if cruise not engaged
* REMOVED: Upload Raw Logs and Full Resolution Videos toggles

sunnypilot - Version 0.8.16 (2022-07-16)
========================
* Sync with commaai's master branches
* NEW❗: Add toggle to pause lateral actuation below 30 MPH / 50 KM/H
* IMPROVED: Better controls mismatch handling
* IMPROVED: Less frequent Low Memory alert
* IMPROVED: Only allow lateral control when in forward gears
* IMPROVED: Better alerts handling on gear changes

sunnypilot - Version 0.8.14-1.3 (2022-06-29)
========================
* Hyundai/Kia/Genesis
    * NEW❗: MADS: Add GAP/Distance button on the steering wheel to engage/disengage
        * To engage/disengage MADS: Hold the button for 0.5 second
* NEW❗: Dynamic Lane Profile: Add toggle to enable "Laneless for Curves in Auto Lane"
* HOTFIX🛠: Improve Torque lateral control and reduce ping pong for some Toyota cars
    * Torque control: higher low speed gains and better steering angle deadzone logic
* Developer UI: Remove Distance Traveled, replace with Memory Usage %
    * This may have a potential to fix the Low Memory alert that may appear

sunnypilot - Version 0.8.14-1 (2022-06-27)
========================
* HOTFIX🛠: Honda, Toyota, Volkswagen now initialized correctly with Torque Lateral Live Tune

sunnypilot - Version 0.8.14-1 (2022-06-27)
========================
* NEW❗: Added toggle to enable updates for sunnypilot
* HOTFIX🛠: Volkswagen car list now displays properly in Force Car Recognition menu
* REVERTED: Honda - temporary removes CRUISE (MAIN) for MADS engagement
    * LKAS button continues to be used for MADS engagement/disengagement

sunnypilot - Version 0.8.14-1 (2022-06-26)
========================
Visit https://bit.ly/sunnyreadme for more details
* sunnypilot 0.8.14 release - based on openpilot 0.8.14 devel
* "0.8.14-prod-c3" branch only supports comma three
    * If you have a comma two, EON, or other devices than a comma three, visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
* Mono-branch support
    * Honda/Acura
    * Hyundai/Kia/Genesis
    * Toyota/Lexus
    * Volkswagen MQB
* Modified Assistive Driving Safety (MADS) Mode
    * NEW❗: CRUISE (MAIN) now engages MADS for all supported car makes
    * NEW❗: Added toggle to disable disengaging Automatic Lane Centering (ALC) on the brake pedal
* Dynamic Lane Profile (DLP)
* NEW❗: Gap Adjust Cruise (GAC)
    * openpilot longitudinal cars can now adjust between the lead car's following distance gap via 3 modes:
        * Steering Wheel (SW) | User Interface (UI) | Steering Wheel + User Interface (SW+UI)
* NEW❗: Custom Camera & Path Offsets
* NEW❗: Torque Lateral Control from openpilot 0.8.15 master (as of 2022-06-15)
* NEW❗: Torque Lateral Control Live Tune Menu
* NEW❗: Speed Limit Sign from openpilot 0.8.15 master (as of 2022-06-22)
* NEW❗: Mapbox Speed Limit data will now be utilized in Speed Limit Control (SLC)
    * Speed limit data will be utilized in the following availability:
        * Mapbox (active navigation) -> OpenStreetMap -> Car Interface (Toyota's TSR)
* Custom Stock Longitudinal Control
    * NEW❗: Volkswagen MQB
    * Honda
    * Hyundai/Kia/Genesis
* NEW❗: Mapbox navigation support for non-Prime users
    * Visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
* Hyundai/Kia/Genesis
    * NEW❗: Enhanced SCC (ESCC) Support
        * Requires hardware modification. Visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
    * NEW❗: Smart MDPS (SMDPS) Support - Auto-detection
        * Requires hardware modification and custom firmware for the SMDPS. Visit sunnyhaibin's discord server for more details: https://discord.gg/wRW3meAgtx
* Toyota/Lexus
    * NEW❗: Added toggle to enforce stock longitudinal control

sunnypilot - Version 0.8.12-4
========================
* NEW❗: Custom Stock Longitudinal Control by setting the target speed via openpilot's "MAX" speed thanks to multikyd!
    * Speed Limit Control
    * Vision-based Turn Control
    * Map-based Turn Control
* NEW❗: HDA status integration with Custom Stock Longitudinal Control on applicable HKG cars only
* NEW❗: Roll Compensation and SteerRatio fix from comma's 0.8.13
* NEW❗: Dev UI to display different metrics on screen
    * Click on the "MAX" box on the top left of the openpilot display to toggle different metrics display
    * Lead car relative distance; Lead car relative speed; Actual steering degree; Desired steering degree; Engine RPM; Longitudinal acceleration; Lead car actual speed; EPS torque; Current altitude; Compass direction
* NEW❗: Stand Still Timer to display time spent at a stop with M.A.D.S engaged (i.e., stop lights, stop signs, traffic congestions)
* NEW❗: Current car speed text turns red when the car is braking
* NEW❗: Export GPS tracks into GPX files and upload to OSM thanks to eFini!
* NEW❗: Enable ACC and M.A.D.S with a single press of the RES+/SET- button
* NEW❗: ACC +/-: Short=5, Long=1
    * Change the ACC +/- buttons behavior with cruise speed change in openpilot
    * Disabled (Stock):  Short=1, Long=5
    * Enabled:  Short=5, Long=1
* NEW❗: Speed Limit Value Offset (not %)*
    * Set speed limit higher or lower than actual speed limit for a more personalized drive.
    * *To use this feature, turn off "Enable Speed Limit % Offset"*
* NEW❗: Dedicated icon to show the status of M.A.D.S.
* NEW❗: No Offroad Fix for non-official devices that cannot shut down after the car is turned off
* NEW❗: Stop N' Go Resume Alternative
    * Offer alternative behavior to auto resume when stopped behind a lead car using stock SCC/ACC. This feature removes the repeating prompt chime when stopped and/or allows some cars to use auto resume (i.e., Genesis)
* IMPROVED: Show the lead car icon in the car's dashboard when a lead car is detected by openpilot's camera vision
* FIXED: MADS button unintentionally set MAX when using stock longitudinal control thanks to Spektor56!

sunnypilot - Version 0.8.12-3
========================
* NEW❗: Bypass "System Malfunction" alert toggle
    * Prevent openpilot from returning the "System Malfunction" alert that hinders the ability use openpilot
* FIXED: Hyundai/Kia/Genesis Brake Hold Active now outputs the correct events on screen with M.A.D.S. engaged

sunnypilot - Version 0.8.12-2
========================
* NEW❗: Disable M.A.D.S. toggle to disable the beloved M.A.D.S. feature
    * Enable Stock openpilot engagement/disengagement
* ADJUST: Initialize Driving Screen Off Brightness at 50%

sunnypilot - Version 0.8.12-1
========================
* sunnypilot 0.8.12 release - based on openpilot 0.8.12 devel
* Dedicated Hyundai/Kia/Genesis branch support
* NEW❗: OpenStreetMap integration thanks to the Move Fast team!
    * NEW❗: Vision-based Turn Control
    * NEW❗: Map-Data-based Turn Control
    * NEW❗: Speed Limit Control w/ optional Speed Limit Offset
    * NEW❗: OpenStreetMap integration debug UI
    * Only available to openpilot longitudinal enabled cars
* NEW❗: Hands on Wheel Monitoring according to EU r079r4e regulation
* NEW❗: Disable Onroad Uploads for data-limited Wi-Fi hotspots when using OpenStreetMap related features
* NEW❗: Fast Boot (Prebuilt)
* NEW❗: Auto Lane Change Timer
* NEW❗: Screen Brightness Control (Global)
* NEW❗: Driving Screen Off Timer
* NEW❗: Driving Screen Off Brightness (%)
* NEW❗: Max Time Offroad
* Improved user feedback with M.A.D.S. operations thanks to Spektor56!
    * Lane Path
        * Green🟢 (Laneful), Red🔴 (Laneless): M.A.D.S. engaged
        * White⚪: M.A.D.S. suspended or disengaged
        * Black⚫: M.A.D.S. engaged, steering is being manually override by user
    * Screen border now only illuminates Green when SCC/ACC is engaged

sunnypilot - Version 0.8.10-1 (Unreleased)
========================
* sunnypilot 0.8.10 release - based on openpilot 0.8.10 `devel`
* Add Toyota cars to Force Car Recognition

sunnypilot - Version 0.8.9-4
========================
* Hyundai: Fix Ioniq Hybrid signals

sunnypilot - Version 0.8.9-3
========================
* Update home screen brand and version structure

sunnypilot - Version 0.8.9-2
========================
* Added additional Sonata Hybrid Firmware Versions
* Features
    * Modified Assistive Driving Safety (MADS) Mode
    * Dynamic Lane Profile (DLP)
    * Quiet Drive 🤫
    * Force Car Recognition (FCR)
    * PID Controller: add kd into the stock PID controller

sunnypilot - Version 0.8.9-1
========================
* First changelog!
* Features
    * Modified Assistive Driving Safety (MADS) Mode
    * Dynamic Lane Profile (DLP)
    * Quiet Drive 🤫
    * Force Car Recognition (FCR)
    * PID Controller: add kd into the stock PID controller
