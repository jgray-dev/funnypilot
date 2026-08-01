"""FunnyPilot v3.5.0 — the speed-limit sign, carrying every SLA state without text.

THE BRIEF: no large text alerts, ever. A state change should be a change in
something already on screen, not a banner arriving from nowhere and covering
the road. The sign is the natural carrier — it is the thing SLA is about, it is
already permanently on screen, and it has an unused perimeter.

  COLOUR says which way the limit is about to move:
      red   a lower zone is coming
      green a higher zone is coming
      cyan  SLA is active and satisfied (nothing pending)
      none  SLA off — plain sign, no halo

  WEIGHT says how close it is. Thickness and bloom grow continuously from a
  hairline at HALO_FAR_M to a solid ring at the boundary, so "a change is
  coming" and "how soon" are one signal instead of two. `halo_spec()` is a pure
  function of (distance, direction) and is unit-tested; the drawing below is
  the only part that needs a screen.

  The upcoming limit slides in beneath at NEXT_SCALE, so "65 now, 45 soon"
  reads as two objects rather than a sentence.

WHAT IS DELIBERATELY UNCHANGED: the MUTCD and Vienna sign faces. Those are
legally recognisable iconography and a driver reads them pre-attentively; a
restyled speed-limit sign is a worse speed-limit sign. Only the frame around
them is new.

NO NEW TEXTURES. The old preActive up/down arrow PNGs are replaced by a chevron
drawn from two lines on the halo's edge — one less asset to fail to load, and
it moves with the halo instead of floating beside it.
"""
import math

import pyray as rl

from openpilot.selfdrive.ui.sunnypilot.onroad.hud import tokens as T

SIGN_W = 155
SIGN_H = 205
NEXT_SCALE = 0.62
GAP = 14
# v3.5.1 — the column is now L-shaped, not a stack. The main sign keeps the
# left; the SLA offset tab and the upcoming sign share a narrower column to its
# right, tab on top. That reads as "this is the sign, and here is what is
# changing about it", and it stops the station growing downward into the road.
TAB_H = 34
SIDE_W = int(SIGN_W * NEXT_SCALE)

HALO_FAR_M = 400.0     # where the halo first becomes visible
HALO_MIN = 0.12        # proximity floor, so "confirmed but distant" still shows
HALO_IDLE = 0.34       # weight when SLA is simply active with nothing pending
HALO_W_MIN = 3.0       # px at proximity 0
HALO_W_MAX = 11.0      # px at the boundary
HALO_PAD = 13          # how far the halo sits outside the sign
BLOOM_PAD = 11         # second, fainter pass

RED = rl.Color(0xFF, 0x5A, 0x52, 255)
GREEN = rl.Color(0x41, 0xE0, 0x8C, 255)
CYAN = rl.Color(0x3A, 0xD8, 0xD8, 255)


def halo_spec(next_limit: float, cur_limit: float, dist_m: float, sla_active: bool):
  """(color, proximity) for the halo, or (None, 0.0) for no halo at all.

  Pure — this is the whole state machine, and it is what the tests pin.
  """
  if next_limit and next_limit > 0 and cur_limit and cur_limit > 0 and abs(next_limit - cur_limit) >= 1:
    if not T.finite(dist_m) or dist_m < 0:
      dist_m = HALO_FAR_M
    prox = max(HALO_MIN, T.clamp(1.0 - dist_m / HALO_FAR_M, 0.0, 1.0))
    return (RED if next_limit < cur_limit else GREEN), prox
  if sla_active:
    return CYAN, HALO_IDLE
  return None, 0.0


class SpeedSign:
  """Draws the sign column. Owns no state that survives a frame except the
  pulse phase, so it cannot get stuck in a stale visual."""

  def __init__(self):
    self._phase = 0.0

  def height(self) -> int:
    """Reserved height. Constant whether or not a second sign is showing —
    nothing below this station may move when one appears. Since v3.5.1 the
    extras sit BESIDE the sign, so the station is exactly one sign tall."""
    return SIGN_H

  def width(self) -> int:
    """Reserved width, likewise constant."""
    return SIGN_W + GAP + SIDE_W

  def render(self, x: float, y: float, *, limit: float, next_limit: float, dist_m: float,
             sla_active: bool, pre_active: bool, offset_ratio: float,
             metric: bool, overspeed: bool, dt: float = 1 / 60.0) -> None:
    self._phase = (self._phase + dt) % 4.0

    color, prox = halo_spec(next_limit, limit, dist_m, sla_active)

    # preActive is the one moment SLA needs the driver to DO something, so it
    # is the one moment the halo moves: a slow pulse plus a direction chevron.
    if pre_active and color is not None:
      prox = T.clamp(prox + 0.30 * (0.5 + 0.5 * math.sin(self._phase * math.pi)), 0.0, 1.0)

    sign_rect = rl.Rectangle(x, y, SIGN_W, SIGN_H)
    if color is not None:
      self._halo(sign_rect, color, prox)

    self._face(sign_rect, limit, metric, overspeed, primary=True)

    # ── the side column: offset tab on top, upcoming sign beneath it ────────
    sx = x + SIGN_W + GAP
    if sla_active:
      self._offset_tab(sx, y, offset_ratio)

    if next_limit and next_limit > 0 and abs(next_limit - (limit or 0)) >= 1:
      nw, nh = SIDE_W, SIGN_H * NEXT_SCALE
      self._face(rl.Rectangle(sx, y + TAB_H + GAP, nw, nh), next_limit, metric, False, primary=False)

    if pre_active:
      self._chevron(sign_rect, next_limit, limit)

  # ── pieces ──────────────────────────────────────────────────────────────

  @staticmethod
  def _halo(rect: rl.Rectangle, color: rl.Color, prox: float) -> None:
    w = T.lerp(HALO_W_MIN, HALO_W_MAX, prox)
    a = T.lerp(0.55, 1.0, prox)

    ring = rl.Rectangle(rect.x - HALO_PAD, rect.y - HALO_PAD,
                        rect.width + HALO_PAD * 2, rect.height + HALO_PAD * 2)
    # bloom first (larger, very transparent), then the ring on top. Two passes
    # is what makes it read as light rather than as a second border.
    bloom = rl.Rectangle(ring.x - BLOOM_PAD, ring.y - BLOOM_PAD,
                         ring.width + BLOOM_PAD * 2, ring.height + BLOOM_PAD * 2)
    rl.draw_rectangle_rounded_lines_ex(bloom, 0.30, 12, w * 2.4, T.with_alpha(color, a * 0.20))
    rl.draw_rectangle_rounded_lines_ex(ring, 0.28, 12, w, T.with_alpha(color, a))

  @staticmethod
  def _face(rect: rl.Rectangle, limit: float, metric: bool, overspeed: bool, primary: bool) -> None:
    val = str(round(limit)) if limit and limit > 0 else "--"
    ink = T.HALT if (overspeed and primary) else T.INK
    scale = rect.width / SIGN_W

    if metric:
      # Vienna: white disc, red annulus
      cx, cy = rect.x + rect.width / 2, rect.y + rect.height / 2
      r = min(rect.width, rect.height) / 2
      rl.draw_circle(int(cx), int(cy), r, T.SIGN_FACE)
      rl.draw_ring(rl.Vector2(cx, cy), r * 0.78, r, 0, 360, 36, RED)
      size = int((62 if len(val) >= 3 else 76) * scale)
      T.text_centered(T.font_bold(), val, cx, cy - size * 0.62, size, ink)
      return

    # MUTCD: white plate, black inner keyline, SPEED / LIMIT, value
    rl.draw_rectangle_rounded(rect, 0.14, 10, T.SIGN_FACE)
    inset = 8 * scale
    inner = rl.Rectangle(rect.x + inset, rect.y + inset,
                         rect.width - inset * 2, rect.height - inset * 2)
    rl.draw_rectangle_rounded_lines_ex(inner, 0.13, 10, max(2.0, 3.5 * scale), T.INK)

    cx = rect.x + rect.width / 2
    lab = int(26 * scale)
    T.text_centered(T.font_semi(), "SPEED", cx, rect.y + 18 * scale, lab, T.INK, 1.5 * scale)
    T.text_centered(T.font_semi(), "LIMIT", cx, rect.y + 46 * scale, lab, T.INK, 1.5 * scale)
    val_sz = int((66 if len(val) >= 3 else 78) * scale)
    T.text_centered(T.font_bold(), val, cx, rect.y + 84 * scale, val_sz, ink)

  @staticmethod
  def _offset_tab(x: float, y: float, ratio: float) -> None:
    pct = ratio * 100.0
    if abs(pct) < 0.5:
      s = "0%"
    else:
      s = f"+{round(pct)}%" if pct > 0 else f"{round(pct)}%"
    # Fills the side column's width rather than shrink-wrapping the text: it
    # sits directly above the upcoming sign and the two must line up.
    rect = rl.Rectangle(x, y, SIDE_W, TAB_H)
    w = SIDE_W
    rl.draw_rectangle_rounded(rect, T.R_PILL, 10, rl.Color(CYAN.r, CYAN.g, CYAN.b, 40))
    rl.draw_rectangle_rounded_lines_ex(rect, T.R_PILL, 10, 2, rl.Color(CYAN.r, CYAN.g, CYAN.b, 140))
    T.text_centered(T.font_bold(), s, rect.x + w / 2, y + 5, T.SZ_LABEL, CYAN, T.TRACK_LABEL)

  @staticmethod
  def _chevron(rect: rl.Rectangle, next_limit: float, cur_limit: float) -> None:
    """Which way to press, drawn on the halo instead of floating beside it.
    Two lines — no texture to load and nothing to fail."""
    up = bool(next_limit and cur_limit and next_limit > cur_limit)
    cx = rect.x + rect.width / 2
    cy = rect.y - HALO_PAD - 20 if up else rect.y + rect.height + HALO_PAD + 20
    s, t = 20.0, 6.0
    col = GREEN if up else RED
    if up:
      rl.draw_line_ex((cx - s, cy + s * 0.55), (cx, cy - s * 0.55), t, col)
      rl.draw_line_ex((cx, cy - s * 0.55), (cx + s, cy + s * 0.55), t, col)
    else:
      rl.draw_line_ex((cx - s, cy - s * 0.55), (cx, cy + s * 0.55), t, col)
      rl.draw_line_ex((cx, cy + s * 0.55), (cx + s, cy - s * 0.55), t, col)
