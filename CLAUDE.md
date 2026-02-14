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
- `selfdrive/controls/lib/latcontrol_torque.py` - Smooth stop logic
- `selfdrive/monitoring/helpers.py` - Driver monitoring timeouts
- `sunnypilot/selfdrive/controls/lib/dec/constants.py` - DEC constants
