# FunnyPilot — working instructions

## Branches and permissions

- Code lives on `funnypilot-X.Y.Z` branches; historical suffixes (`a`, `b`, `e`,
  `st`) are distinct cuts. `master` is a documentation-only directory, not code
  to develop, install or flash. Never merge its file-removal commit into a release.
- Continue on the user's requested version. New versions get a new branch based
  on the explicitly selected prior version, not `master` or upstream. If starting
  from `master`, follow its branch-selection instructions, then read that branch's
  `CLAUDE.md` and `AGENTS.md` before working. Highest version does not imply tested.
- Push to `funnypilot` (`git@github.com:jgray-dev/funnypilot.git`). In this checkout
  `origin` is sunnypilot upstream; verify remotes before pushing.
- **Standing owner authorization: commit and push completed project work to the
  fork's remote Git host without asking again.** A Git push does not deploy to the
  vehicle. This does not authorize force-pushing unrelated history, deleting
  unrelated branches, or touching upstream.
- **Device flashing, restart/reboot, or changing device settings requires an
  explicit request.** Never infer it from permission to push. Do not create
  `PUSH<version>.sh` unless requested.
- On a version bump, align `FUNNYPILOT_VERSION` (number only), webserver
  `EXPECTED_VERSION`, branch name and `CHANGELOG.md`. Keep this file short: current
  rules and current Key Files only. Update `master`'s current-development pointer.
  Put detailed investigations in `docs/`, not here.

## Before investigating or editing

- Read `AGENTS.md`; for driving/steering/braking issues follow
  `docs/feedback-workflow.md` and query the private feedback inbox first.
  Use actual recorded identity/events, not labels alone. Synthetic checks are not
  driving evidence. State when device data is unavailable.
- Preserve unrelated/uncommitted work. Inspect targets before destructive changes.
- No credentials, private recordings or device `cloud.json` in Git or logs.
- Historical rationale: `CHANGELOG.md`, focused `docs/` reports, or
  `git show <older-version-or-commit>:CLAUDE.md`. Do not reload the entire history
  unless the task needs it. The old comprehensive notes are at `950736eeb:CLAUDE.md`.

## Load-bearing implementation rules

- No new schema, compiled-code, Params-key, asset or shader changes casually:
  these can force a device rebuild. Python-only changes still need import checks.
- Evaluated annotations must not apply `|` to capnp objects or pyray factories.
  These are not necessarily Python types. Keep the repo's AST annotation guard.
- UI modules are on the boot path. `hud/` must do no IO at import/construction;
  lazy-load IO, use `tokens.safe_draw`, reuse shared visual tokens, and do not nest
  raylib scissor regions. Video and model projection must use the same viewport.
- Never hide all normal-severity alerts: calibration/no-entry faults use normal.
  Quiet only explicit routine event/type pairs; preserve takeover priority.
  Project geometry only with fresh, valid, current-drive calibrated data.
- Control-loop work must be bounded and avoid durable `/data` IO. Use existing
  bounded shared-memory/worker patterns; no blocking uploads or diagnostics.
- Age received services with `sm.recv_time` (consumer monotonic clock), not epoch
  timestamps or mixed publisher clocks. Compare filesystem mtimes to wall time.
- MPC owns following/braking; do not add competing downstream acceleration
  overrides or delay braking for comfort. Keep SCC cap, throttle gate and ribbon
  consistent. Do not bypass calibration, service-health or vehicle safety gates.
- One PID update per tick in the selected unit system. Preserve reset, handback,
  integral freeze and EPS limits. Do not add causal EMA/FIR filtering to lateral
  knots: past-output filtering previously moved maneuver onset.
- K5 `SAS_Speed` is unsigned; use the existing signed angle-derived motion helper.
  Confirm physical hypotheses from measurements, not plausible sensor stories.
- Diagnostics must become reachable before background housekeeping. Keep CPU,
  memory, retries and logs bounded. Saved drives are hard deletion exclusions;
  corrupt retention metadata must pause deletion, not mean "nothing saved".

## Current version: 3.7.3e — Key Files

Based on 3.7.2 `5ec74a3c7`. Setup, architecture and validation:
`docs/astra-link-3.7.3e.md`; wire contract: `docs/astra-link-protocol.md`.
Historical 3.7.2 audit remains in `docs/readiness-audit-3.7.2.md`.

- `sunnypilot/astra_link/{daemon,core,state,files,execution}.py`: optional
  outbound HTTPS client; private config, bounded reads, fresh ignition-off
  command gate, no-replay receipts and expiring execution leases. Imports inert.
- `tools/astra_link.py`: explicitly authorized local pairing/enable/disable.
  Never expose `/data/astra_link/config.json` or reuse the host bridge secret.
- `system/manager/process_config.py`, `selfdrive/selfdrived/selfdrived.py`:
  optional `funnypilot_astra` joins `mapd`/`funnypilot_feedback` exclusions.
  Required processes, upstream `feedbackd` and camera gates remain unchanged.
- Astra server source: `/home/astro/sandbox/astra-web` (not a Git repository).
  Native session/MCP, hardware auth/approvals and UI changes are preserved in
  `docs/astra-web-3.7.3e.patch` with a source-hash manifest. No global model change.
- Device logs are untrusted evidence. Compare full commit/branch/dirty state,
  and obtain recording-time identity; current disk state is not running-code proof.
  Never auto-switch shared/dirty local code to match the device.
- Website/Worker deployed; broker restart and device pairing/flash remain pending.
  No driving validation. Broker restart terminates active PTYs; authorize separately.
  No controller retuning, schema changes, new Params keys or on-device LLM.

## Verification and device access

- Use Python 3.11+; existing host environment: `/tmp/fp-audit-venv/bin/python`.
  Import-light pytest flags: `PYTHONPATH=. python -m pytest --noconftest -q
  -p no:cacheprovider -o addopts='' <tests>`. Full regression command and native-only
  exclusions are in `docs/controls-review-3.7.1a.md`. Do not trust leaked mock
  modules as substitutes for native IPC/Params/acados tests.
- Run relevant production-path tests, fault injection, Ruff and diagnostic marker
  checks. Make important regression tests fail with the old bug. Do not copy the
  implementation into tests or use assertions satisfiable by comments.
- Home SSH: `ssh comma@192.168.86.31`; checkout: `/data/openpilot`. A manager/UI
  crash does not necessarily stop SSH. Saved Wi-Fi reconnects without UI; a
  hotspot using a known SSID/password can restore access. Inspect swaglog traceback
  first; bare system Python may lack the device venv's dependencies.
- On device, never run Git under sudo. Root-owned checkout failures require
  fixing ownership of the whole tree, not only `.git`. For Git CA errors, use
  local `http.sslCAInfo=/etc/ssl/certs/ca-certificates.crt`; never disable TLS checks.
- After an explicitly authorized deploy, verify the version **and commit hash**,
  then verify manager/UI health and listening port 8888 after startup. A successful
  push or shell exit is not proof of a successful flash or safe vehicle behavior.
