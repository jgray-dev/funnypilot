"""FunnyPilot v3.6.2 — the left and right edges become blinker and blind spot.

The state glow already puts engagement state in peripheral vision. The SIDES of
that glow are the natural place for the two things that are inherently about a
side of the car, and which were previously only ever shown as icons: which way
you are signalling, and whether the lane you are signalling into is occupied.

  blinker only   the edge pulses AMBER at the blinker's own cadence
  blind spot     the edge shows a RED band positioned at the blind-spot zone
  both           RED, pulsing. Signalling into an occupied lane is the case
                 that most deserves to be hard to miss, so it gets the colour
                 of the hazard and the rhythm of the intent rather than one or
                 the other.

────────────────────────────────────────────────────────────────────────────
WHAT THIS CAN AND CANNOT KNOW ABOUT WHERE THE OTHER CAR IS

The request was for the red band to track the other vehicle's position and move
with it. IT CANNOT, AND PRETENDING OTHERWISE WOULD BE THE WORST OPTION
AVAILABLE, so it is worth being exact about why:

  * `carState.leftBlindspot` / `rightBlindspot` are BOOLEANS. On this car they
    come from `LCA11.CF_Lca_IndLeft` — the rear corner radars publish an
    indicator bit on CAN and nothing else. No range, no rate, no count.
  * the interior camera faces the DRIVER. It is the driver-monitoring camera;
    openpilot publishes `driverStateV2` (face and pose) from it and nothing
    about the world outside. It has no view of an adjacent lane.
  * the Mando radar this fork enabled in v3.3.0e is FORWARD facing. It can see
    a car in the next lane while that car is still ahead of us, and loses it
    long before the blind spot begins.

So `position` is an OPTIONAL input, defaulting to None, and None means "the
zone, not a vehicle": the band sits over the physical blind-spot region, which
is a real place beside the car even when its occupant's position is unknown.
What DOES move is the entry and exit — the band slides in from behind when the
flag asserts and slides back out when it clears, at a rate a car changing lanes
actually moves — so it reads as something arriving rather than a lamp coming
on. If a source of real longitudinal position ever exists (a rear radar that
publishes range, or a side camera), it feeds `position` and the band tracks it
with no other change.

Pure and stdlib-only: no pyray, no IO. The drawing lives in chrome.py.
"""
import math

# Blinker cadence. Road vehicle lighting regulations put turn signals at
# 60-120 flashes per minute; 1.5 Hz sits in the middle and, more usefully,
# matches the relay the driver can hear.
BLINK_HZ = 1.5
BLINK_MIN_A = 0.18      # the pulse dips to here rather than to nothing, so the
BLINK_MAX_A = 0.85      # side stays continuously readable while signalling

BSD_ALPHA = 0.92        # a hazard, not a status: brighter than the blinker

# Where the blind-spot zone sits on the edge, as a fraction of frame height
# measured from the top. The zone is beside and slightly behind the driver, and
# the frame is a forward-facing view, so it belongs BELOW centre — the further
# down the edge, the closer to the car's own flank.
BSD_CENTRE = 0.72
BSD_EXTENT = 0.34       # fraction of frame height the band covers

# How fast the band slides in and out, in fractions of frame height per second.
# Sized from the thing it represents: a car closing at 5 m/s covers the ~6 m of
# blind-spot zone in a bit over a second, so a band that takes about that long
# to arrive reads as motion rather than as a fade.
SLIDE_RATE = 0.55
# Where the band comes FROM and goes TO — off the bottom of the edge, i.e. from
# behind the car. Entering from the front would be the overtaking case, which
# we cannot distinguish, and being wrong about direction is worse than being
# consistent about it.
SLIDE_FROM = 1.15

ALPHA_TAU = 0.12        # s; fast enough to feel instant, slow enough not to snap

KIND_NONE = 0
KIND_BLINKER = 1
KIND_BSD = 2


def _finite(x) -> bool:
  try:
    f = float(x)
  except (TypeError, ValueError):
    return False
  return f == f and abs(f) != float('inf')


def blink_alpha(phase: float) -> float:
  """The pulse envelope. A raised cosine rather than a square wave: a hard
  on/off at 1.5 Hz in peripheral vision is a strobe, and the whole point of
  putting this at the edge of vision is that it must be noticeable without
  being an alarm."""
  if not _finite(phase):
    return BLINK_MAX_A
  t = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
  return BLINK_MIN_A + (BLINK_MAX_A - BLINK_MIN_A) * t


class SideSignal:
  """One edge. Fed every frame; exposes what to draw there.

  `centre` and `extent` are fractions of frame height, `alpha` is 0..1, and
  `kind` says which palette to use. A caller that draws nothing when
  `alpha <= 0` is doing the right thing — the object settles to exactly zero
  rather than to an asymptote, so an idle edge costs no draw calls at all.
  """

  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self.kind = KIND_NONE
    self.alpha = 0.0
    self.centre = BSD_CENTRE
    self.extent = BSD_EXTENT
    self._phase = 0.0
    self._slide = SLIDE_FROM      # 1.15 = fully off the bottom, i.e. not here
    self._presence = 0.0          # eased 'this edge has something to say'
    self._target_p = 0.0

  def update(self, dt: float, blinker: bool, blindspot: bool,
             position: float | None = None) -> None:
    """`position` is the other vehicle's place in the zone, 0 = alongside the
    mirror, 1 = at the back of the zone. None — the normal case on this car —
    means the position is not sensed and the band represents the ZONE. See the
    module docstring for why nothing available can supply it."""
    if not _finite(dt) or dt <= 0.0:
      dt = 0.0
    dt = min(dt, 0.25)      # a stalled frame must not teleport anything

    blinker = bool(blinker)
    blindspot = bool(blindspot)

    self._phase = (self._phase + dt * BLINK_HZ) % 1.0 if (blinker and dt) else self._phase
    if not blinker:
      self._phase = 0.0

    # THE EASING AND THE PULSE ARE SEPARATE, AND CONFLATING THEM IS A BUG.
    # An EMA over the finished alpha does not smooth a transition, it
    # ATTENUATES AND DELAYS THE WAVEFORM: a first-order lag with tau 0.12 s
    # against a 1.5 Hz pulse has a gain of 0.66, so the peak never arrives and
    # what should be a blinker reads as a dim flicker. `presence` is the thing
    # that eases — an edge arriving or leaving — and the waveform multiplies it.
    if blindspot:
      self.kind = KIND_BSD
      # RED WINS THE COLOUR, THE BLINKER WINS THE RHYTHM. Signalling into an
      # occupied lane is the one combination that is actually dangerous, and
      # showing it as a steady red would make it the quietest of the three.
      wave = blink_alpha(self._phase) if blinker else BSD_ALPHA
      self._target_p = 1.0
    elif blinker:
      self.kind = KIND_BLINKER
      wave = blink_alpha(self._phase)
      self._target_p = 1.0
    else:
      wave = 0.0
      self._target_p = 0.0

    # slide: toward the zone while occupied, back off the bottom when clear
    want = 0.0 if blindspot else SLIDE_FROM
    step = SLIDE_RATE * dt
    if self._slide < want:
      self._slide = min(want, self._slide + step)
    else:
      self._slide = max(want, self._slide - step)

    a = 1.0 - math.exp(-dt / ALPHA_TAU) if (dt > 0.0 and ALPHA_TAU > 0) else 1.0
    self._presence += (self._target_p - self._presence) * a
    if abs(self._presence - self._target_p) < 0.004:
      self._presence = self._target_p      # SETTLE, or an idle edge draws forever
    self.alpha = self._presence * wave
    if self.alpha <= 0.0:
      self.alpha = 0.0
      if self._presence <= 0.0:
        self.kind = KIND_NONE

    if self.kind == KIND_BLINKER:
      # a turn signal is about the whole side, not a place on it
      self.centre, self.extent = 0.5, 1.0
    else:
      pos = position if (position is not None and _finite(position)) else 0.5
      pos = min(max(float(pos), 0.0), 1.0)
      # `pos` walks the band across the zone; `_slide` walks the whole zone off
      # the bottom of the edge when nothing is there.
      base = BSD_CENTRE + (pos - 0.5) * BSD_EXTENT
      self.centre = base + self._slide * 0.5
      self.extent = BSD_EXTENT
