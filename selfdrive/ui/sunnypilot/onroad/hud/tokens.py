"""FunnyPilot v3.5.0 — the onroad HUD's design tokens and its blast shield.

WHY THIS MODULE EXISTS AT ALL. Before v3.5.0 the onroad screen had eleven
widgets and eleven visual languages: the set-speed box used roundness 0.35 with
a 6 px white-at-75 border, the road name used 0.2 with black-at-120, the SCC
badges carried their own palette and a private 6-frame lerp, and the dev bar
used no container. Every constant lived next to the code that drew it, so
"make it consistent" was not a change you could make. Everything visual is now
defined here and imported.

SAFETY, WHICH OUTRANKS EVERY AESTHETIC DECISION IN THIS PACKAGE.
`selfdrive/ui/ui.py` draws BOTH offroad and onroad from one process. If an
onroad widget raises, the exception unwinds through MainLayout into the render
loop and kills the process; manager restarts it; it dies on the next onroad
frame. That is a UI boot-loop, and a device with no UI cannot reach settings,
which is how you would flash your way out of it. So:

  * every module in this package imports only pyray, the stdlib, and repo
    modules that were already on the UI's import path;
  * nothing touches the filesystem, /dev/shm or a texture at import time or in
    a constructor — only lazily, inside a guarded draw;
  * `safe_draw` wraps every new widget. The FIRST exception from a widget logs
    a full traceback and permanently disables that widget for the session. One
    broken readout costs you that readout, never the screen.

`safe_draw` is deliberately not clever: no retry, no backoff, no re-enable. A
widget that threw once is not trusted again until the process restarts, because
the alternative is an exception every frame at 60 Hz filling the log and
burning the CPU budget the rest of the UI needs.
"""
import math
import time

import pyray as rl

from openpilot.common.swaglog import cloudlog
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached

# ── engagement state ──────────────────────────────────────────────────────
# Hues are the product's own (they match BORDER_COLORS and the MADS states);
# they are normalised to a similar luminance so they read as one set rather
# than as five unrelated colours.
ENGAGED = rl.Color(0x41, 0xE0, 0x8C, 255)
LAT_ONLY = rl.Color(0x3A, 0xD8, 0xD8, 255)
LONG_ONLY = rl.Color(0xA2, 0x5C, 0xD1, 255)
OVERRIDE = rl.Color(0xFF, 0xB4, 0x54, 255)
DISENGAGED = rl.Color(0x5C, 0x70, 0x85, 255)

# ── semantic (separate from state; only ever means out-of-nominal) ────────
ATTENTION = rl.Color(0xFF, 0xB4, 0x54, 255)
HALT = rl.Color(0xFF, 0x5A, 0x52, 255)
NOMINAL = rl.Color(0x8F, 0xA3, 0xB8, 255)

WHITE = rl.Color(255, 255, 255, 255)
# v3.7.0 introduced the follow-distance line as a pale cyan-white "hologram";
# v3.7.1 made it a minimal guide bar at the owner's request: NEUTRAL WHITE, no
# hue at all. Its colour now carries exactly one piece of information — how far
# inside the desired gap the lead is — as a blend from this toward HALT (the
# fork's one red). White-to-red only, never toward green: the bar reports a
# reason to slow, and nothing on it may ever read as "accelerate".
GUIDE = rl.Color(0xF2, 0xF4, 0xF6, 255)
HOLO = GUIDE   # the v3.7.0 name, kept for the marker table and any old reader
INK = rl.Color(0x0B, 0x0F, 0x14, 255)
SIGN_FACE = rl.Color(0xF2, 0xF4, 0xF6, 255)

# ── surfaces ──────────────────────────────────────────────────────────────
SCRIM = rl.Color(8, 11, 15, 158)        # 62% — every plate on the screen
HAIRLINE = rl.Color(255, 255, 255, 33)  # 13% — the one top-edge highlight
MUTED = rl.Color(255, 255, 255, 130)
FAINT = rl.Color(255, 255, 255, 108)

# ── form ──────────────────────────────────────────────────────────────────
# v3.5.1 — the steering indicator is ambient, not a readout. It sits at the
# bottom of the frame under the driver's line of sight, so it is held well
# below full strength; its job is to be noticed, not read.
TORQUE_OPACITY = 0.55

# v3.5.4 — THREE RADII, AND ONLY THREE. Before this there were four ad-hoc
# values living next to the code that drew them (0.13, 0.14, 0.28, 0.30 in
# speed_sign.py), which is the same drift tokens.py was created to stop.
# Mixed corner radii do not read as a decision, they read as carelessness.
R_CHIP = 0.14       # small faces and keylines
R_PLATE = 0.26      # plates, chips, and the sign halo
R_PILL = 0.5        # full
BORDER_W = 2
PAD = 40            # station gutter

# The implied light comes from ABOVE. A uniform hairline on all four sides
# reads as an OUTLINE; a brighter line inset along the top edge reads as a lit
# surface. One extra draw call per plate, and it is what makes the remaining
# containers look intentional rather than boxed.
SPECULAR = rl.Color(255, 255, 255, 64)

# ── type ──────────────────────────────────────────────────────────────────
# NOTE these are NOMINAL sizes: gui_app multiplies both draw_text_ex and
# measure_text_cached by FONT_SCALE (1.242 on the big UI), so a nominal 186
# lands at ~231 px. They are kept in the same units as the rest of the repo
# so numbers here are comparable with the code they replace.
SZ_DISPLAY = 186
SZ_TITLE = 88
SZ_DATA = 46
SZ_LABEL = 27
SZ_MICRO = 22
TRACK_LABEL = 3.6   # letter spacing for uppercase labels, in px


def font_bold() -> rl.Font:
  return gui_app.font(FontWeight.BOLD)


def font_semi() -> rl.Font:
  return gui_app.font(FontWeight.SEMI_BOLD)


def font_med() -> rl.Font:
  return gui_app.font(FontWeight.MEDIUM)


# ── maths ─────────────────────────────────────────────────────────────────

def clamp(v: float, lo: float, hi: float) -> float:
  return lo if v < lo else (hi if v > hi else v)


def smoothstep(u: float) -> float:
  u = clamp(u, 0.0, 1.0)
  return u * u * (3.0 - 2.0 * u)


def lerp(a: float, b: float, t: float) -> float:
  return a + (b - a) * clamp(t, 0.0, 1.0)


def lerp_color(a: rl.Color, b: rl.Color, t: float) -> rl.Color:
  t = clamp(t, 0.0, 1.0)
  return rl.Color(int(a.r + (b.r - a.r) * t), int(a.g + (b.g - a.g) * t),
                  int(a.b + (b.b - a.b) * t), int(a.a + (b.a - a.a) * t))


def with_alpha(c: rl.Color, a: float) -> rl.Color:
  return rl.Color(c.r, c.g, c.b, int(clamp(a, 0.0, 1.0) * 255))


def finite(x) -> bool:
  return isinstance(x, (int, float)) and math.isfinite(x)


# ── text ──────────────────────────────────────────────────────────────────

def text_at(font, s: str, x: float, y: float, size: int, color, spacing: float = 0.0) -> float:
  """Draw at (x, y) top-left. Returns the drawn width."""
  rl.draw_text_ex(font, s, rl.Vector2(float(x), float(y)), size, spacing, color)
  return measure_text_cached(font, s, size, spacing).x


def text_centered(font, s: str, cx: float, y: float, size: int, color, spacing: float = 0.0) -> float:
  w = measure_text_cached(font, s, size, spacing).x
  rl.draw_text_ex(font, s, rl.Vector2(float(cx - w / 2), float(y)), size, spacing, color)
  return w


def text_shadowed(font, s: str, x: float, y: float, size: int, color, spacing: float = 0.0) -> float:
  """Legibility without a plate: the same glyphs drawn twice, black underneath.

  This is what lets the hero speed sit directly on the camera image. Two draw
  calls is materially cheaper than the alternative (a blurred scrim texture)
  and it survives a bright, cluttered scene, which bare white type does not.
  """
  rl.draw_text_ex(font, s, rl.Vector2(float(x + 2), float(y + 3)), size, spacing, rl.Color(0, 0, 0, 190))
  return text_at(font, s, x, y, size, color, spacing)


def text_centered_shadowed(font, s: str, cx: float, y: float, size: int, color, spacing: float = 0.0) -> float:
  w = measure_text_cached(font, s, size, spacing).x
  return text_shadowed(font, s, cx - w / 2, y, size, color, spacing)


def plate(rect: rl.Rectangle, roundness: float = R_PLATE) -> None:
  """The one container in the system: a 62% scrim, a hairline edge, and a
  specular highlight along the top (v3.5.4 — see SPECULAR)."""
  rl.draw_rectangle_rounded(rect, roundness, 10, SCRIM)
  rl.draw_rectangle_rounded_lines_ex(rect, roundness, 10, BORDER_W, HAIRLINE)
  # inset so the highlight sits ON the surface rather than on its edge, and
  # short of the corners so it does not fight the rounding
  inset = max(6.0, rect.width * 0.12)
  y = rect.y + BORDER_W + 1.0
  rl.draw_line_ex((rect.x + inset, y), (rect.x + rect.width - inset, y), 2.0, SPECULAR)


# ── motion ────────────────────────────────────────────────────────────────
# v3.5.4 — ONE TIME CONSTANT FOR THE WHOLE SCREEN.
#
# Before this, almost nothing transitioned: pills appeared and vanished on a
# frame, the sign halo jumped the instant a zone was confirmed, the engagement
# colour snapped. Each of those is a small visual shock, and a screen full of
# them reads as a set of widgets rather than one instrument.
#
# Everything that APPEARS, DISAPPEARS or CHANGES VALUE now goes through Eased,
# on a shared tau, so the whole HUD settles on one clock. This is the same
# decision the minimap's pose smoothing made in v3.5.1, applied to the rest.
#
# FRAME-RATE INDEPENDENT BY CONSTRUCTION: alpha comes from exp(-dt/tau), not
# from a fixed per-frame fraction. A fixed fraction silently changes the feel
# whenever the frame rate moves, which on a device that throttles is a bug that
# only appears when it is hot.
EASE_TAU = 0.18       # s, the house time constant
EASE_SNAP = 0.002     # settle exactly, so a value can reach its target
_EASE_DT_MAX = 0.25   # a paused/stalled frame must not teleport the value


class Eased:
  """First-order ease toward a target. Pure apart from the clock."""

  def __init__(self, value: float = 0.0, tau: float = EASE_TAU):
    self.x = float(value)
    self.tau = float(tau)
    self._last = 0.0

  def snap(self, value: float) -> float:
    """Jump with no transition — for re-engage and other discontinuities where
    there is no continuity worth preserving."""
    self.x = float(value)
    return self.x

  def update(self, target: float, now: float | None = None) -> float:
    t = time.monotonic() if now is None else now
    dt = (t - self._last) if self._last else (1.0 / 60.0)
    self._last = t
    if not finite(target):
      return self.x
    dt = clamp(dt, 0.0, _EASE_DT_MAX)
    a = 1.0 - math.exp(-dt / self.tau) if self.tau > 0.0 else 1.0
    self.x += (float(target) - self.x) * a
    if abs(float(target) - self.x) < EASE_SNAP:
      self.x = float(target)
    return self.x


class EasedColor:
  """Eases the four channels of a colour together, so a state change is a
  cross-fade rather than a cut."""

  def __init__(self, color: rl.Color, tau: float = EASE_TAU):
    self._c = [float(color.r), float(color.g), float(color.b), float(color.a)]
    self._e = [Eased(v, tau) for v in self._c]

  def update(self, color: rl.Color, now: float | None = None) -> rl.Color:
    tgt = (color.r, color.g, color.b, color.a)
    vals = [int(clamp(e.update(float(v), now), 0.0, 255.0)) for e, v in zip(self._e, tgt, strict=True)]
    return rl.Color(*vals)


# ── the blast shield ──────────────────────────────────────────────────────
_disabled: set[str] = set()


def safe_draw(name: str, fn, *args, **kwargs) -> None:
  """Run one widget's drawing. The first failure disables it for the session.

  See the module docstring: an unguarded raise here is a UI boot-loop, and a
  device without a UI is a device you cannot flash from.
  """
  if name in _disabled:
    return
  try:
    fn(*args, **kwargs)
  except Exception:
    _disabled.add(name)
    try:
      cloudlog.exception(f"onroad hud widget '{name}' raised; disabled for this session")
    except Exception:
      pass


def is_disabled(name: str) -> bool:
  return name in _disabled


def reset_disabled() -> None:
  """Tests only."""
  _disabled.clear()
