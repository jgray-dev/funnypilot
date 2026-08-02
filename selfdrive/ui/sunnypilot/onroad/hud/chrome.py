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
_BLACK = rl.Color(0, 0, 0, 255)   # hoisted: allocating this per frame is pure waste
_BAND_INK = (6, 9, 13)

# v3.5.1: how many nested outlines make up one falloff. 4 px steps over a 120 px
# glow is 30 draws — smooth to the eye, and cheap because each is a rect
# outline, not a filled quad.
_STEP_PX = 4


# v3.5.6 — THE RINGS ARE BUILT ONCE AND REDRAWN, NOT REBUILT EVERY FRAME.
#
# The vignette is 55 rings and the glow another 30, and each one was allocating
# a fresh `rl.Rectangle` and `rl.Color` through cffi on every single frame —
# 255 allocations a frame for a gradient whose inputs are constant most of the
# time. The rect never changes size; the colour and alpha come from easers that
# SETTLE, so once a transition finishes every frame produces byte-identical
# geometry and byte-identical bytes.
#
# The cache key is the quantised result, not the raw floats: alpha reaches the
# framebuffer as an integer anyway, so two float alphas that round to the same
# byte are the same picture and must share an entry — otherwise the easer's
# last few thousandths would miss the cache forever. Bounded because the key
# includes the colour, and a slow cross-fade walks through many of them.
_RING_CACHE: dict = {}
_RING_CACHE_MAX = 24


def _rings(rect: rl.Rectangle, depth: int, color: rl.Color, alpha0: float):
  key = (int(rect.x), int(rect.y), int(rect.width), int(rect.height),
         depth, color.r, color.g, color.b, int(clamp(alpha0, 0.0, 1.0) * 255))
  hit = _RING_CACHE.get(key)
  if hit is not None:
    return hit

  d = int(min(depth, max(1, int(rect.width) // 2), max(1, int(rect.height) // 2)))
  steps = max(1, d // _STEP_PX)
  a0 = clamp(alpha0, 0.0, 1.0)
  out = []
  for i in range(steps):
    inset = i * _STEP_PX
    # linear falloff, evaluated at the middle of the ring so the first ring is
    # not drawn at full alpha for its whole 4 px width
    t = (inset + _STEP_PX * 0.5) / d
    a = int(a0 * max(0.0, 1.0 - t) * 255)
    if a <= 0:
      continue
    w = rect.width - inset * 2
    h = rect.height - inset * 2
    if w <= 0 or h <= 0:
      break
    out.append((rl.Rectangle(rect.x + inset, rect.y + inset, w, h),
                rl.Color(color.r, color.g, color.b, a)))

  if len(_RING_CACHE) >= _RING_CACHE_MAX:
    _RING_CACHE.clear()
  _RING_CACHE[key] = out
  return out


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
  for ring, col in _rings(rect, depth, color, alpha0):
    rl.draw_rectangle_lines_ex(ring, _STEP_PX, col)


# v3.5.4 — SCENE-ADAPTIVE CHROME.
#
# The vignette and bands were tuned for daylight and applied at full strength
# regardless. At night the camera image is already dark, so a 72% vignette plus
# a 350 px 80% top scrim is far heavier than it needs to be and eats road view
# for nothing.
#
# `deviceState.screenBrightnessPercent` is driven by the ambient light sensor
# via hardwared's auto-brightness, and the UI already subscribes to deviceState
# (ui_state.py:47) — so this costs no new signal and no new param.
#
# FLOORED WELL ABOVE ZERO, DELIBERATELY. The vignette is LOAD-BEARING for the
# state glow: v3.5.0 established that a glow drawn straight onto a bright sky
# washes out completely, which is exactly when you most want to know whether
# the car is steering. Scaling it to nothing at night would trade one failure
# for another, so the floor is a real constraint and not a taste value.
CHROME_MIN = 0.55


def chrome_scale(brightness_pct: float) -> float:
  """Chrome strength for an ambient brightness, 0..100 -> CHROME_MIN..1.0.

  Pure and unit-tested. Any garbage input returns 1.0 — full chrome is the
  daylight-safe answer, so an unreadable sensor degrades to today's behaviour.
  """
  if not isinstance(brightness_pct, (int, float)) or brightness_pct != brightness_pct:
    return 1.0
  return CHROME_MIN + (1.0 - CHROME_MIN) * clamp(float(brightness_pct) / 100.0, 0.0, 1.0)


def draw_vignette(rect: rl.Rectangle, scale: float = 1.0) -> None:
  """Darken the frame edges. MUST run before draw_state_glow."""
  _edges(rect, VIG_DEPTH, _BLACK, VIG_ALPHA * clamp(scale, 0.0, 1.0))


def draw_state_glow(rect: rl.Rectangle, color: rl.Color, intensity: float = 1.0) -> None:
  """Engagement state as an ambient edge bloom.

  intensity is the breathing term — 1.0 normally, oscillating slightly while
  overriding so 'I am not steering right now' is felt rather than read.
  """
  _edges(rect, GLOW_DEPTH, color, clamp(GLOW_ALPHA * intensity, 0.0, 1.0))


def draw_bands(rect: rl.Rectangle, scale: float = 1.0) -> None:
  """The two scrims chrome is allowed to live in."""
  x, y = int(rect.x), int(rect.y)
  w, h = int(rect.width), int(rect.height)
  s = clamp(scale, 0.0, 1.0)

  top = min(BAND_TOP_H, h)
  rl.draw_rectangle_gradient_v(x, y, w, top,
                               rl.Color(6, 9, 13, int(BAND_TOP_A * s * 255)), _BLANK)

  bot = min(BAND_BOT_H, h)
  rl.draw_rectangle_gradient_v(x, y + h - bot, w, bot,
                               _BLANK, rl.Color(6, 9, 13, int(BAND_BOT_A * s * 255)))
