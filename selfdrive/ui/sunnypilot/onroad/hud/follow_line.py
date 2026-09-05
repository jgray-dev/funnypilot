"""FunnyPilot v3.7.0 / v3.7.1 — the follow-distance guide bar.

A line drawn across the road, on the road, at the gap the longitudinal planner
is holding to. When a lead's chevron sits on the line we are at the distance
the MPC wants; a lead INSIDE the line is closer than the planner wants, and the
line says so by turning red. It answers the question a driver actually has when
following — "is the car going to close up or back off from here?" — without a
number.

v3.7.1 MADE IT MINIMAL, at the owner's request, and changed what it means:

  * NEUTRAL WHITE (tokens.GUIDE), no hue. The v3.7.0 cyan "hologram" read as a
    projection; the owner wanted a guide, not an effect.
  * NO END POSTS. The line is one stroke and a soft halo, nothing else.
  * SHOWN WITHOUT A LEAD. The gap is the planner's regardless of whether radar
    has a track, and a bar on an empty road tells the driver where a car ahead
    would have to be before the planner started to care.
  * WHITE -> RED, AND NOTHING ELSE. A lead inside the line is the planner
    wanting more gap — the only thing this bar is allowed to say. It never
    tints toward green: nothing on it may ever read as "accelerate". The tint is
    GEOMETRIC, `tint_for()` — how far inside the desired gap the lead is — because
    the line is drawn at the gap the MPC's cost pulls toward, so a lead inside it
    is by construction a lead the planner would rather have further away.

WHERE THE DISTANCE COMES FROM. Not here. plannerd publishes it over
/dev/shm/fp_follow (see long_v2/scc_shm.py): `t_follow * v_ego + STOP_DISTANCE`,
with `t_follow` already folding in the personality and the low-speed cushion.
The UI draws a number the controller chose; it does not recompute it.

WHERE THE LINE GOES, AND WHY THAT IS THE WHOLE POINT. "As accurate as possible"
means the line has to sit where a lead at that gap would actually appear on the
screen, and the naive answer — x metres straight ahead, at road level — is wrong
in exactly the situations where following matters:

  * IN A CURVE the lead is not straight ahead. The line follows the MODEL PATH
    to arc-length `gap`, so it bends into the corner with the road, and it is
    oriented along the path's NORMAL there, so it stays perpendicular to the
    lane rather than to the car.
  * ON A HILL the road at 40 m is not at the height of the road under the car.
    The line takes its z from the model path at that distance plus the
    calibrated camera height — which is exactly how `model_renderer` places the
    lead chevron, and going through the same `_car_space_transform` is what
    guarantees the two agree to the pixel.

`follow_line_segment` and `tint_for` are pure and tested without a screen;
`FollowLine` owns the easing and the drawing. Nothing in this module does IO
at import or in a constructor (the hud/ rule), and the /dev/shm read is a lazy
import inside the draw.
"""
import math
import time

import numpy as np
import pyray as rl

from openpilot.selfdrive.ui.sunnypilot.onroad.hud import tokens as T

# Half the line's span across the road, metres. A lane is ~3.7 m and the car
# ~1.85; this is wide enough to read as a gate across the lane, narrow enough
# not to reach into the next one on a two-lane road.
HALF_WIDTH_M = 1.45
# The line fades in and out on the same house time constant as everything else
# on this HUD; the distance eases slightly slower so speed jitter at 20 Hz does
# not shimmer the line up and down the road.
GAP_TAU = 0.30
# How far down the road the line may be drawn. Past this the model path is
# uncertain and a line there would claim a precision it does not have.
MAX_GAP_M = 120.0
# One /dev/shm read per publish period, shared across frames.
_READ_PERIOD_S = 0.05
# v3.7.1 — TWO strokes, not three, and quieter: a soft halo for contrast
# against a bright road, and the line. The v3.7.0 glow was (0.10, 0.20, 0.62).
_STROKES = ((2.2, 0.12), (1.0, 0.48))
# v3.7.1 — the tint. FULLY red when the lead is at this fraction of the desired
# gap; white at the line. At half the gap the MPC is braking in earnest, which
# is what full red should mean.
TINT_FULL_FRAC = 0.5


def follow_line_segment(path_pts: np.ndarray, gap_m: float, half_width: float = HALF_WIDTH_M):
  """Two 3-D points, left and right, across the model path at `gap_m` metres.

  `path_pts` is the model path as (N, 3) car-frame points — x forward, y (with
  the camera offset already applied, as model_renderer stores it), z. Returns
  `(left, right)` as (3,) arrays in that same frame, or None when the path does
  not reach that far or the inputs are unusable.

  Pure. The normal is taken from the local path direction so the segment is
  perpendicular to the LANE at that point; in a curve that is visibly different
  from perpendicular to the car, and it is the difference between a gate the
  lead drives through and a line it crosses at an angle.
  """
  try:
    pts = np.asarray(path_pts, dtype=np.float64)
    g = float(gap_m)
  except (TypeError, ValueError):
    return None
  if pts.ndim != 2 or pts.shape[1] != 3 or pts.shape[0] < 2:
    return None
  if not math.isfinite(g) or g <= 0.0 or not math.isfinite(half_width) or half_width <= 0.0:
    return None
  xs = pts[:, 0]
  if not np.all(np.isfinite(pts)) or g > xs[-1] or g < xs[0]:
    return None

  # The segment of the path straddling the gap. `searchsorted` needs x to be
  # non-decreasing, which the model path is by construction.
  i = int(np.searchsorted(xs, g, side="right")) - 1
  i = max(0, min(i, pts.shape[0] - 2))
  p0, p1 = pts[i], pts[i + 1]
  span = p1[0] - p0[0]
  f = 0.0 if span <= 1e-9 else (g - p0[0]) / span
  f = min(max(f, 0.0), 1.0)
  centre = p0 + f * (p1 - p0)

  # Path direction in the ground plane, then the left-hand normal. If two
  # consecutive points coincide (a degenerate path), fall back to straight
  # ahead rather than dividing by zero.
  tx, ty = float(p1[0] - p0[0]), float(p1[1] - p0[1])
  n = math.hypot(tx, ty)
  if n < 1e-9:
    nx, ny = 0.0, 1.0
  else:
    nx, ny = -ty / n, tx / n
  offset = np.array([nx * half_width, ny * half_width, 0.0])
  return centre + offset, centre - offset


def tint_for(gap_m: float, lead: bool, d_rel_m: float) -> float:
  """How red the bar is, 0 (white) .. 1 (HALT). Pure. v3.7.1.

  Zero without a lead, zero for a lead ON or BEYOND the line, and rising
  linearly as the lead moves inside it, reaching 1.0 at `TINT_FULL_FRAC` of
  the gap. ONE-SIDED BY CONSTRUCTION: there is no branch for a lead further
  away than the line, because the bar is not allowed to suggest acceleration.
  Garbage reads as white — a bad radar frame must not flash the bar red.
  """
  try:
    g, d = float(gap_m), float(d_rel_m)
  except (TypeError, ValueError):
    return 0.0
  if not lead or not (math.isfinite(g) and math.isfinite(d)) or g <= 0.0:
    return 0.0
  span = g * TINT_FULL_FRAC
  if span <= 0.0:
    return 0.0
  return T.clamp((g - d) / span, 0.0, 1.0)


def _project(transform: np.ndarray, p):
  """Car-frame (x, y, z) -> screen (x, y), or None if behind the camera."""
  v = transform @ np.array([p[0], p[1], p[2]], dtype=np.float64)
  if abs(v[2]) < 1e-6 or v[2] < 0.0:
    return None
  return float(v[0] / v[2]), float(v[1] / v[2])


class FollowLine:
  """Owns the easing and the draw. Construct freely: no IO, no screen."""

  def __init__(self):
    self._alpha = T.Eased(0.0)
    self._gap = T.Eased(0.0, tau=GAP_TAU)
    self._tint = T.Eased(0.0)
    self._cache = (0.0, 0.0, False)
    self._cache_t = 0.0

  def _read(self):
    now = time.monotonic()
    if now - self._cache_t >= _READ_PERIOD_S:
      try:
        from openpilot.sunnypilot.selfdrive.controls.lib.long_v2.scc_shm import read_follow_shm
        self._cache = read_follow_shm()
      except Exception:
        self._cache = (0.0, 0.0, False)
      self._cache_t = now
    return self._cache

  def target(self, long_active: bool):
    """(alpha target, gap target). Split out so the visibility rule is testable
    without a screen: shown while longitudinal is active AND the planner is
    publishing a gap. v3.7.1: a lead is NOT required — the bar marks the gap the
    planner would hold, whether or not anything is there to hold it to."""
    gap, _tf, _lead = self._read()
    show = bool(long_active) and 0.0 < gap <= MAX_GAP_M
    return (1.0 if show else 0.0), (gap if show else self._gap.x)

  def draw(self, model_renderer, sm, rect: rl.Rectangle) -> None:
    """Called once per frame from the road view, AFTER the model renderer, so
    its transform, path and clip region are this frame's. The easers read the
    clock themselves — call this exactly once per frame (see tokens.Eased)."""
    try:
      long_active = bool(sm['carControl'].longActive)
    except Exception:
      long_active = False
    a_t, g_t = self.target(long_active)
    alpha = self._alpha.update(a_t)
    gap = self._gap.update(g_t)

    # v3.7.1 — the lead's actual position, for the tint. Read off radarState
    # rather than off the shm channel so the tint is this frame's, not the
    # planner's last publish; the channel's own lead flag is not needed here.
    lead, d_rel = False, 0.0
    try:
      l1 = sm['radarState'].leadOne
      lead, d_rel = bool(l1.status), float(l1.dRel)
    except Exception:
      lead, d_rel = False, 0.0
    tint = self._tint.update(tint_for(gap, lead, d_rel) if a_t > 0.0 else 0.0)
    if alpha < 0.02:
      return

    path = getattr(model_renderer, "_path", None)
    pts = getattr(path, "raw_points", None)
    transform = getattr(model_renderer, "_car_space_transform", None)
    if pts is None or transform is None or len(pts) < 2:
      return
    z_off = float(getattr(model_renderer, "_path_offset_z", 0.0))

    seg = follow_line_segment(pts, gap)
    if seg is None:
      return
    left, right = seg
    left = left + np.array([0.0, 0.0, z_off])
    right = right + np.array([0.0, 0.0, z_off])

    L = _project(transform, left)
    R = _project(transform, right)
    if L is None or R is None:
      return
    # Both endpoints must land inside the road view; a line half off-screen
    # would draw across the chrome.
    for (x, y) in (L, R):
      if not (rect.x <= x <= rect.x + rect.width and rect.y <= y <= rect.y + rect.height):
        return

    # Thickness follows perspective: a gate 30 m out is thinner on screen than
    # one 10 m out, and scaling with the projected span is what makes that
    # true without a second projection.
    span_px = math.hypot(R[0] - L[0], R[1] - L[1])
    base = min(max(span_px * 0.022, 1.5), 9.0)

    # White toward the fork's one red, by how far inside the line the lead is.
    # Both ends are tokens; the blend is the only colour arithmetic here.
    colour = T.lerp_color(T.GUIDE, T.HALT, tint)
    lv = rl.Vector2(L[0], L[1])
    rv = rl.Vector2(R[0], R[1])
    for mult, a in _STROKES:
      rl.draw_line_ex(lv, rv, base * mult, T.with_alpha(colour, a * alpha))
