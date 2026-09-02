"""FunnyPilot v3.7.0 — the follow-distance hologram.

A line drawn across the road, on the road, at the gap the longitudinal planner
is holding to behind the lead. When the lead's chevron sits on the line, we are
at the distance the MPC wants; ahead of it we are closer than that, behind it
further. It answers the question a driver actually has when following — "is the
car going to close up or back off from here?" — without a number.

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

`follow_line_segment` is the pure geometry and is tested without a screen;
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
# End posts, metres. Short — they exist to make the line read as standing on
# the road surface rather than painted on the windscreen, and to show the
# height the model thinks the road has there.
POST_H_M = 0.35
# The line fades in and out on the same house time constant as everything else
# on this HUD; the distance eases slightly slower so speed jitter at 20 Hz does
# not shimmer the line up and down the road.
GAP_TAU = 0.30
# How far down the road the line may be drawn. Past this the model path is
# uncertain and a line there would claim a precision it does not have.
MAX_GAP_M = 120.0
# One /dev/shm read per publish period, shared across frames.
_READ_PERIOD_S = 0.05
# Rendering: three stacked strokes give the glow; the core is the last.
_GLOW = ((3.2, 0.10), (1.9, 0.20), (1.0, 0.62))


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
    without a screen: shown only while longitudinal is active AND a lead is
    tracked AND the planner is publishing a gap."""
    gap, _tf, lead = self._read()
    show = bool(long_active) and bool(lead) and 0.0 < gap <= MAX_GAP_M
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

    lv = rl.Vector2(L[0], L[1])
    rv = rl.Vector2(R[0], R[1])
    for mult, a in _GLOW:
      rl.draw_line_ex(lv, rv, base * mult, T.with_alpha(T.HOLO, a * alpha))

    # End posts: the same endpoint lifted by POST_H_M in car space, projected.
    # They stand the line up on the road.
    for foot, foot_v in ((left, lv), (right, rv)):
      top = _project(transform, foot + np.array([0.0, 0.0, POST_H_M]))
      if top is not None:
        rl.draw_line_ex(foot_v, rl.Vector2(top[0], top[1]), max(base * 0.8, 1.5),
                        T.with_alpha(T.HOLO, 0.55 * alpha))
