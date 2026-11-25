import math

import numpy as np

# FunnyPilot: lateral torque scale vs speed. 0% at standstill, 50% at 5 mph, 100% at >= 10 mph.
TAPER_BP = [0., 4.4704]  # m/s (0, 10 mph)
TAPER_V = [0., 1.]


def low_speed_torque_scale(v_ego: float) -> float:
  if not math.isfinite(v_ego):
    return 0.
  return float(np.interp(v_ego, TAPER_BP, TAPER_V))
