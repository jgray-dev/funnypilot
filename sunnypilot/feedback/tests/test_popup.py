import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS


def test_taps_save_multiple_labels_without_submit_and_retry_after_done(monkeypatch):
  clock=[100.]
  sent=[]
  def rect(x=0,y=0,width=0,height=0):
    return NS(x=x,y=y,width=width,height=height)
  ray=ModuleType('pyray')
  ray.Rectangle=rect
  ray.check_collision_point_rec=lambda p,r:r.x <= p.x <= r.x+r.width and r.y <= p.y <= r.y+r.height
  ray.draw_rectangle_rounded=lambda *a:None
  monkeypatch.setitem(sys.modules,'pyray',ray)
  application=ModuleType('openpilot.system.ui.lib.application')
  application.gui_app=NS(mouse_events=[],font=lambda *a:None)
  application.FontWeight=NS(MEDIUM=1)
  monkeypatch.setitem(sys.modules,application.__name__,application)
  tokens=ModuleType('openpilot.selfdrive.ui.sunnypilot.onroad.hud.tokens')
  for name in ('plate','text_centered','text_at'):
    setattr(tokens,name,lambda *a:None)
  for name in ('R_CHIP','WHITE','MUTED','LAT_ONLY','HAIRLINE','INK'):
    setattr(tokens,name,1)
  monkeypatch.setitem(sys.modules,tokens.__name__,tokens)
  hud=importlib.import_module('openpilot.selfdrive.ui.sunnypilot.onroad.hud')
  monkeypatch.setattr(hud,'tokens',tokens,raising=False)
  source=Path(__file__).resolve().parents[3]/'selfdrive/ui/sunnypilot/onroad/feedback_popup.py'
  spec=importlib.util.spec_from_file_location('_feedback_popup_test',source)
  module=importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
  monkeypatch.setattr(module.P,'send_report',lambda i,l,t:sent.append(list(l)) or True)
  monkeypatch.setattr(module.P,'read_json',lambda *a:{})
  popup=module.FeedbackPopup()
  parent=rect(0,0,1920,1080)

  def draw(): popup.draw(parent)
  def tap(key):
    draw()
    r=next(r for k,r in popup._buttons if k==key)
    point=NS(x=r.x+r.width/2,y=r.y+r.height/2)
    for pressed in (True,False):
      application.gui_app.mouse_events=[NS(slot=0,pos=point,left_pressed=pressed,left_released=not pressed)]
      draw()
    application.gui_app.mouse_events=[]

  tap('open')
  assert sent[-1]==[]
  tap('steering_bite')
  tap('unnecessary_slowdown')
  assert sent[-1]==['steering_bite','unnecessary_slowdown']
  tap('done')
  before=len(sent)
  clock[0]+=.6
  draw()
  assert len(sent)>before and sent[-1]==['steering_bite','unnecessary_slowdown']
  popup.draw(parent,alert=True)
  assert popup.rect is None
