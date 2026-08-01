"""FunnyPilot v3.5.0 — the remaining stations: set speed, status pills, vitals.

A STATION IS AN ADDRESS, NOT A PRIORITY. Every element on this screen has a
fixed place that is reserved whether or not it currently has anything to say.
That invariant is the whole reorganisation, and it is not cosmetic: the old
dev-UI rail computed `gap_width = (available - total) / num_gaps` over a
CONDITIONALLY built list, so the moment `liveDelay` dropped validity every
remaining metric slid sideways. Nothing here reflows. A station with nothing to
report draws nothing and keeps its space.

THE STATUS STRIP replaces the scattered SCC/SLA badges. One row of identical
pills under the speed: same height, same radius, same type. Lit means that
source is currently governing; muted means it is watching. A driver learns one
shape instead of four.

THE VITALS are ambient — length and light, never text. The left spine is
acceleration (green up, red down from the centre); the dot beneath it is the
longitudinal state, which since v3.4.4 is the car's actual brake lamp rather
than an inference from commanded accel.
"""
import pyray as rl

from openpilot.system.ui.lib.text_measure import measure_text_cached

from openpilot.selfdrive.ui.sunnypilot.onroad.hud import tokens as T

SET_W = 168
SET_H = 200

PILL_H = 52
PILL_PAD = 22
PILL_GAP = 14

SPINE_W = 17
SPINE_H = 500


def draw_set_speed(x: float, y: float, value: float, is_set: bool, icbm_value=None) -> None:
  """The cruise set speed. When ICBM is moving the cluster the label slot shows
  the cluster's own value instead of the word — same station, same size, so the
  swap costs no layout."""
  rect = rl.Rectangle(x, y, SET_W, SET_H)
  T.plate(rect)
  cx = x + SET_W / 2

  if icbm_value is not None:
    T.text_centered(T.font_semi(), str(round(icbm_value)), cx, y + 18, T.SZ_DATA, T.MUTED)
  else:
    T.text_centered(T.font_semi(), "SET", cx, y + 26, T.SZ_LABEL, T.MUTED, T.TRACK_LABEL)

  txt = str(round(value)) if is_set else "–"
  col = T.WHITE if is_set else rl.Color(255, 255, 255, 110)
  T.text_centered(T.font_bold(), txt, cx, y + 74, T.SZ_TITLE, col)


def draw_speed(cx: float, y: float, value: float) -> None:
  """The hero. No plate — a shadow instead, which is what buys back the ~110 px
  of road view the old boxed treatment cost.

  v3.5.1: NO UNIT LABEL. It never changes on a given car, so it carried no
  information — and because it sat beside the number, the number itself was
  offset from centre by half the label's width, breaking alignment with the
  road name above and the status pills below. The number is now centred on cx
  and the whole column lines up.
  """
  s = str(round(value))
  T.text_centered_shadowed(T.font_bold(), s, cx, y, T.SZ_DISPLAY, T.WHITE)


def draw_road_name(cx: float, y: float, name: str) -> None:
  if not name:
    return
  s = name.upper()
  if len(s) > 34:
    s = s[:33] + "…"
  T.text_centered_shadowed(T.font_bold(), s, cx, y, T.SZ_LABEL, T.FAINT, 5.0)


class Pill:
  __slots__ = ("text", "color", "lit")

  def __init__(self, text: str, color=None, lit: bool = False):
    self.text = text
    self.color = color or T.MUTED
    self.lit = lit


def draw_status_strip(cx: float, y: float, pills: list) -> None:
  """One centred row. Empty list draws nothing and reserves its height."""
  if not pills:
    return
  f = T.font_bold()
  widths = [measure_text_cached(f, p.text, T.SZ_LABEL, T.TRACK_LABEL).x + PILL_PAD * 2 + 26
            for p in pills]
  total = sum(widths) + PILL_GAP * (len(pills) - 1)
  x = cx - total / 2

  for p, w in zip(pills, widths, strict=True):
    rect = rl.Rectangle(x, y, w, PILL_H)
    if p.lit:
      rl.draw_rectangle_rounded(rect, T.R_PILL, 10, T.with_alpha(p.color, 0.14))
      rl.draw_rectangle_rounded_lines_ex(rect, T.R_PILL, 10, 2, T.with_alpha(p.color, 0.55))
    else:
      rl.draw_rectangle_rounded(rect, T.R_PILL, 10, T.SCRIM)
      rl.draw_rectangle_rounded_lines_ex(rect, T.R_PILL, 10, 2, T.HAIRLINE)
    dot_x = x + PILL_PAD + 6
    rl.draw_circle(int(dot_x), int(y + PILL_H / 2), 6.0, p.color)
    T.text_at(f, p.text, dot_x + 16, y + 12, T.SZ_LABEL, p.color, T.TRACK_LABEL)
    x += w + PILL_GAP


def draw_accel_spine(x: float, cy: float, accel: float) -> None:
  """Acceleration as length from the centre. Green up, red down."""
  track = rl.Rectangle(x, cy - SPINE_H / 2, SPINE_W, SPINE_H)
  rl.draw_rectangle_rounded(track, T.R_PILL, 8, rl.Color(255, 255, 255, 18))

  mag = T.clamp(abs(accel) / 2.5, 0.0, 1.0) * (SPINE_H / 2 - 6)
  if mag < 2.0:
    return
  if accel > 0:
    bar = rl.Rectangle(x, cy - mag, SPINE_W, mag)
    col = T.ENGAGED
  else:
    bar = rl.Rectangle(x, cy, SPINE_W, mag)
    col = T.HALT
  rl.draw_rectangle_rounded(bar, T.R_PILL, 8, T.with_alpha(col, 0.85))


def draw_long_dot(x: float, y: float, state: str) -> None:
  """green = throttle, gray = coasting or inactive, red = brake lamp lit.

  (x, y) is the dot's CENTRE since v3.5.1 — it lives in the bottom-left corner
  now, where a top-left anchor would have made the margin depend on the glyph
  box rather than on the thing you actually see.
  """
  col = {"green": T.ENGAGED, "red": T.HALT}.get(state, rl.Color(126, 138, 153, 255))
  rl.draw_circle(int(x), int(y), 16.0, T.with_alpha(col, 0.28))
  rl.draw_circle(int(x), int(y), 11.0, col)
  T.text_at(T.font_bold(), "LONG", x + 26, y - 11, T.SZ_MICRO, T.FAINT, 3.2)
