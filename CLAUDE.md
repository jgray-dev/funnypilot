# FunnyPilot Development Notes

## SSH Access

Device: `ssh comma@192.168.86.31`

## Git Remotes

- `funnypilot` - git@github.com:jgray-dev/funnypilot.git (push here)
- `origin` - sunnypilot upstream

## Branching Policy (v0.9.5+)

All versions must be separate branches: `funnypilot-0.9.X`
In each new branch, modify the CHANGELOG.md file to correspond to what was last modified in the version of the branch.
Additionally, edit CLAUDE.md Key Files section to describe what changes and logic were implemented in what files.

## Key Files

- `FUNNYPILOT_VERSION` - Version number only. No changelog.

### v0.9.6 Changes

- `selfdrive/controls/lib/latcontrol_torque.py` - Smooth stop (15mph threshold) + lane change torque ramping (50% → 100% over 2s)
- `selfdrive/monitoring/helpers.py` - Driver monitoring (3x original timeouts: 90s passive, 33s active)
- `selfdrive/controls/lib/longitudinal_planner.py` - Max acceleration cap (70% of original values)
- `selfdrive/controls/radard.py` - Lead vehicle smoothing (5-frame moving average for vLead/aLead, 3-frame for dRel)
- `sunnypilot/selfdrive/controls/lib/smart_cruise_control/vision_controller.py` - SCC-V gas gating (early gas cut at 0.8 lat acc, gentler decel, 3s lookahead)
- `sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py` - SCC-M gas gating (gentler jerk/accel, coast preference, early offset)

### Previous Versions

- `sunnypilot/selfdrive/controls/lib/dec/constants.py` - DEC constants (older versions)
