# FunnyPilot - working instructions

- Version: see `FUNNYPILOT_VERSION` (number only). Notes for this cut: `docs/funnypilot-4.0.0.md`.
- Base: sunnypilot `v2026.002.003` (openpilot 0.11.2), AGNOS 18.4.
- Code lives on `funnypilot-X.Y.Z` branches.
- Push only to remote `funnypilot` (`git@github.com:jgray-dev/funnypilot.git`). `origin`
  is sunnypilot upstream: never push there. Verify remotes before pushing.
- Device flashing, restart/reboot or changing device settings requires an explicit user
  request. A git push is not a deploy.
- Never commit credentials, device `cloud.json`, or private recordings.
- UI is on the boot path: `hud`/UI modules must do no IO at import or construction.
- Reader budget: never add a `carState` or `deviceState` subscriber (msgq caps readers
  per service; exceeding it breaks every reader and engagement).
- No casual schema (cereal capnp), Params-key, compiled-code or asset changes.
- Verify with `python -m py_compile` and `ruff check` on touched files; run focused tests
  with `PYTHONPATH=. python -m pytest --noconftest -q -p no:cacheprovider -o addopts='' <tests>`.
