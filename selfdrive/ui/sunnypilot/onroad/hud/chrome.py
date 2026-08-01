"""FunnyPilot v3.5.0 — screen chrome: vignette, state glow, horizon bands.

THE STATE GLOW replaces the solid coloured ring the border used to be. The ring
occupied 30 px of frame and said one thing; the glow says the same thing in
peripheral vision, over the part of the camera image that carries the least
information, and leaves room for it to say more — it can BREATHE while the
driver is overriding, which is a channel a static ring does not have.

  * 60% alpha at the very edge, falling linearly to zero over GLOW_DEPTH px.
  * All four edges. Corners are two overlapping gradients and therefore read
    brighter; that is kept deliberately — it frames the image.

THE VIGNETTE IS LOAD-BEARING, NOT DECORATION. A glow drawn straight onto a
bright sky washes out completely — the exact failure that makes an ambient
state indicator untrustworthy, because it is invisible in precisely the
conditions where you most want to know whether the car is steering. So a
darkening pass is drawn FIRST, deeper than the glow (VIG_DEPTH) and stronger
(VIG_ALPHA), giving the glow a dark ground to sit on in every scene. Order is
the whole trick: vignette, then glow, then the bands, then content.

THE HORIZON BANDS come from what the camera actually shows — sky at the top,
road and traffic through the middle, hood at the bottom. Chrome lives in the
top and bottom strata and the middle third is never drawn into. They are
gradient scrims rather than plates, so there is no visible box edge anywhere.

Everything here is raylib rectangle gradients: eight for the vignette and glow,
two for the bands. No textures, no shaders, no allocation per frame.
"""
import pyray as rl

from openpilot.selfdrive.ui.sunnypilot.onroad.hud.tokens import clamp

GLOW_DEPTH = 120     # px, edge -> transparent
GLOW_ALPHA = 0.60    # at the edge
VIG_DEPTH = 220      # px, deeper than the glow so the glow always has a ground
VIG_ALPHA = 0.72

BAND_TOP_H = 350
BAND_BOT_H = 138
BAND_TOP_A = 0.80
BAND_BOT_A = 0.78

_BLANK = rl.Color(0, 0, 0, 0)

# v3.5.1: how many nested outlines make up one falloff. 4 px steps over a 120 px
# glow is 30 draws — smooth to the eye, and cheap because each is a rect
# outline, not a filled quad.
_STEP_PX = 4


def _edges(rect: rl.Rectangle, depth: int, color: rl.Color, alpha0: float) -> None:
  """An EVEN inward falloff on all four edges, as nested rectangle outlines.

  WHY NOT FOUR GRADIENTS, which is what v3.5.0 did: a full-width top gradient
  and a full-height left gradient OVERLAP in the corner, so the corner is
  composited twice and reads brighter — and because the overlap region is
  `depth` square while the edges are thousands of pixels long, the eye reads it
  as the rail fading out unevenly towards the corners rather than as a bright
  corner. Reported from the car as the left ends of the top and bottom rails
  fading off more than the rest.

  Nested outlines have no overlap by construction: every pixel belongs to
  exactly one ring, and its alpha is a function of its distance from the
  nearest edge — which is the definition of an even falloff. Corners are
  automatically consistent with the sides.
  """
  d = int(min(depth, max(1, int(rect.width) // 2), max(1, int(rect.height) // 2)))
  steps = max(1, d // _STEP_PX)
  a0 = clamp(alpha0, 0.0, 1.0)

  for i in range(steps):
    inset = i * _STEP_PX
    # linear falloff, evaluated at the middle of the ring so the first ring is
    # not drawn at full alpha for its whole 4 px width
    t = (inset + _STEP_PX * 0.5) / d
    a = int(a0 * max(0.0, 1.0 - t) * 255)
    if a <= 0:
      continue
    ring = rl.Rectangle(rect.x + inset, rect.y + inset,
                        rect.width - inset * 2, rect.height - inset * 2)
    if ring.width <= 0 or ring.height <= 0:
      break
    rl.draw_rectangle_lines_ex(ring, _STEP_PX, rl.Color(color.r, color.g, color.b, a))


def draw_vignette(rect: rl.Rectangle) -> None:
  """Darken the frame edges. MUST run before draw_state_glow."""
  _edges(rect, VIG_DEPTH, rl.Color(0, 0, 0, 255), VIG_ALPHA)


def draw_state_glow(rect: rl.Rectangle, color: rl.Color, intensity: float = 1.0) -> None:
  """Engagement state as an ambient edge bloom.

  intensity is the breathing term — 1.0 normally, oscillating slightly while
  overriding so 'I am not steering right now' is felt rather than read.
  """
  _edges(rect, GLOW_DEPTH, color, clamp(GLOW_ALPHA * intensity, 0.0, 1.0))


def draw_bands(rect: rl.Rectangle) -> None:
  """The two scrims chrome is allowed to live in."""
  x, y = int(rect.x), int(rect.y)
  w, h = int(rect.width), int(rect.height)

  top = min(BAND_TOP_H, h)
  rl.draw_rectangle_gradient_v(x, y, w, top,
                               rl.Color(6, 9, 13, int(BAND_TOP_A * 255)), _BLANK)

  bot = min(BAND_BOT_H, h)
  rl.draw_rectangle_gradient_v(x, y + h - bot, w, bot,
                               _BLANK, rl.Color(6, 9, 13, int(BAND_BOT_A * 255)))
