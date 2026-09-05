"""Tap once per label. Report delivery is nonblocking; saved state is acknowledged."""
import time
import uuid
import pyray as rl

from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.selfdrive.ui.sunnypilot.onroad.hud import tokens as T
from openpilot.sunnypilot.feedback import protocol as P


class FeedbackPopup:
  def __init__(self):
    self.event_id = None
    self.started = 0.0
    self.until = 0.0
    self.labels = []
    self.message = ''
    self.saved = False
    self.rect = None
    self._poll_at = 0.0
    self._send_at = 0.0
    self._ack = []
    self._pressed = None
    self._buttons = []

  def open(self):
    self.event_id = uuid.uuid4().hex
    self.started = time.monotonic()
    self.until = self.started + 15
    self.labels, self._ack = [], []
    self.saved = False
    self.message = 'Saving drive...'
    self._send_at = 0.0
    self._send()

  def _send(self):
    if self.event_id is not None:
      self._send_at = time.monotonic()
      if not P.send_report(self.event_id, self.labels, self.started):
        self.message = 'Capture unavailable; retrying...'

  def blocks_touch(self):
    if self.rect is None:
      return False
    return any(rl.check_collision_point_rec(e.pos, self.rect) for e in gui_app.mouse_events)

  def layout(self, parent):
    scale = min(parent.width / 1920, parent.height / 1080)
    opened = time.monotonic() < self.until
    w, h = (760, 300) if opened else (150, 65)
    self.rect = rl.Rectangle(parent.x + parent.width - (w + 35)*scale,
                             parent.y + parent.height - (h + 125)*scale, w*scale, h*scale)
    return scale, opened

  def draw(self, parent, alert=False):
    scale, opened = self.layout(parent)
    if alert:
      self.rect = None
      return  # never cover or compete with a driving alert
    now = time.monotonic()
    if self.event_id is not None and now - self.started <= 20 and now - self._poll_at >= .1:
      self._poll_at = now
      status = P.read_json(P.STATUS, {})
      if isinstance(status, dict) and status.get('id') == self.event_id:
        self.saved = bool(status.get('saved'))
        self._ack = status.get('labels', [])
        self.message = status.get('message', '')
      if (not self.saved or set(self._ack) != set(self.labels)) and now-self._send_at >= .5:
        self._send()
    r = self.rect
    T.plate(r)
    font = gui_app.font(FontWeight.MEDIUM)
    size = max(16, int(27*scale))
    self._buttons = []
    if not opened:
      T.text_centered(font, 'Report', r.x+r.width/2, r.y+17*scale, size, T.WHITE)
      self._buttons.append(('open', r))
    else:
      T.text_at(font, 'What felt wrong? Select any.', r.x+18*scale, r.y+14*scale, size, T.WHITE)
      done = rl.Rectangle(r.x+r.width-100*scale, r.y+5*scale, 95*scale, 50*scale)
      T.text_centered(font, 'Done', done.x+done.width/2, done.y+10*scale, size, T.MUTED)
      self._buttons.append(('done', done))
      for index, (key, label) in enumerate(P.LABELS.items()):
        x, y = index % 2, index // 2
        chip = rl.Rectangle(r.x+(14+x*370)*scale, r.y+(64+y*57)*scale, 360*scale, 49*scale)
        rl.draw_rectangle_rounded(chip, T.R_CHIP, 8, T.LAT_ONLY if key in self.labels else T.HAIRLINE)
        T.text_centered(font, label, chip.x+chip.width/2, chip.y+10*scale, size, T.INK if key in self.labels else T.WHITE)
        self._buttons.append((key, chip))
      T.text_at(font, self.message[:70], r.x+18*scale, r.y+252*scale, max(14, int(20*scale)), T.MUTED)
    for event in gui_app.mouse_events:
      if event.slot != 0:
        continue
      hit = next((key for key, rect in self._buttons if rl.check_collision_point_rec(event.pos, rect)), None)
      if event.left_pressed:
        self._pressed = hit
      if event.left_released:
        if hit is not None and hit == self._pressed:
          if hit == 'open':
            self.open()
          elif hit == 'done':
            self.until = 0.0
          elif hit not in self.labels:
            self.labels.append(hit)
            self.message = 'Saving selection...'
            self._send()
        self._pressed = None
