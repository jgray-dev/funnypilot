"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
LIMIT_ADAPT_ACC = -0.7  # m/s^2 Ideal acceleration for the adapting (braking) phase when approaching speed limits.
# s. Extra runway ahead of the braking phase. Covers the set speed change propagating to the car (button spam,
# PCM acknowledge, driver confirmation) so the taper has already started by the time braking needs to begin.
LIMIT_ADAPT_REACTION_TIME = 4.
LIMIT_ADAPT_MIN_DISTANCE = 40.  # m. Floor for the runway, so small speed deltas still get a usable lead in.
LIMIT_MAX_MAP_DATA_AGE = 10.  # s Maximum time to hold to map data, then consider it invalid inside limits controllers.

# Speed Limit Assist constants
PCM_LONG_REQUIRED_MAX_SET_SPEED = {
  True: (33.3333, 36.1111),  # km/h, (120, 130)
  False: (31.2928, 35.7632),  # mph, (70, 80)
}

CONFIRM_SPEED_THRESHOLD = {
  True: 80,   # km/h
  False: 50,  # mph
}
