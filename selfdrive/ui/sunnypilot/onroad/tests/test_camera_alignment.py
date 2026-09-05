"""Project real image points through production camera and model transforms."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[5]


def method(path, name, scope):
  tree = ast.parse((ROOT/path).read_text())
  node = next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
  unit = ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
  exec(compile(ast.fix_missing_locations(unit),str(path),'exec'),scope)
  return scope[name]


def rect(x,y,width,height):
  return NS(x=x,y=y,width=width,height=height)


@pytest.mark.parametrize('wide', [False, True])
@pytest.mark.parametrize('sidebar', [0, 300])
def test_video_pixels_and_projected_geometry_agree(wide, sidebar):
  intrinsic=np.array([[567 if wide else 2648,0,964],[0,567 if wide else 2648,604],[0,0,1.]])
  calib=np.array([[0,1,0],[0,0,1],[1,0,0.]])
  # A nonzero mounting yaw changes the vanishing point; same calibration must
  # position both texture and geometry, for each camera and sidebar width.
  yaw=.04
  rotation=np.array([[np.cos(yaw),-np.sin(yaw),0],[np.sin(yaw),np.cos(yaw),0],[0,0,1.]])
  calib=calib@rotation
  scope={'np':np,'rl':NS(Rectangle=rect),'WIDE_CAM':2,'TICI':True,
         'INF_POINT':np.array([1000.,0,0]),'VisionStreamType':NS(VISION_STREAM_DRIVER=99),
         'ui_state':NS(sm=NS(recv_frame={'liveCalibration':20}))}
  camera=NS(fcam=NS(intrinsics=intrinsic),ecam=NS(intrinsics=intrinsic))
  scope['DEFAULT_DEVICE_CAMERA']=camera
  transform=method('selfdrive/ui/onroad/augmented_road_view.py','_calc_frame_matrix',scope)
  draw=method('selfdrive/ui/onroad/cameraview.py','_render',scope)
  self=NS(_matrix_cache_key=None,_cached_matrix=None,device_camera=camera,stream_type=2 if wide else 1,
          view_from_calib=calib,view_from_wide_calib=calib,
          model_renderer=NS(set_transform=lambda matrix:setattr(self,'projection',matrix)),
          frame=NS(width=1928,height=1208),_switching=False,_stream_type=2 if wide else 1,
          _ensure_connection=lambda:True, client=NS(recv=lambda **kw:None,is_connected=lambda:True),
          _render_egl=lambda src,dst:setattr(self,'destination',dst))
  self._calc_frame_matrix=lambda r:transform(self,r)
  outer=rect(sidebar,0,2160-sidebar,1080)
  self._content_rect=rect(outer.x+30,30,outer.width-60,1020)
  # Use the actual rectangle expression passed by AugmentedRoadView. Restoring
  # the old outer rect makes the two numerical projections disagree.
  tree=ast.parse((ROOT/'selfdrive/ui/onroad/augmented_road_view.py').read_text())
  call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and
            n.func.attr=='_render' and isinstance(n.func.value,ast.Call) and isinstance(n.func.value.func,ast.Name) and
            n.func.value.func.id=='super')
  viewport=eval(compile(ast.Expression(call.args[0]),'<camera viewport>','eval'),{}, {'self':self,'rect':outer})
  draw(self,viewport)
  for point in (np.array([30.,-2.,1.4]),np.array([70.,3.,0.7])):
    pixel=intrinsic@calib@point
    pixel=pixel[:2]/pixel[2]
    dst=self.destination
    image_point=np.array([dst.x+pixel[0]*dst.width/1928, dst.y+pixel[1]*dst.height/1208])
    overlay=self.projection@point
    np.testing.assert_allclose(image_point,overlay[:2]/overlay[2],atol=1e-9)
  previous=self.projection.copy()
  self._content_rect.x += 40  # same dimensions, different screen origin
  transform(self,self._content_rect)
  assert not np.array_equal(previous,self.projection)
