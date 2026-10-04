# FunnyPilot — find the working version first

This default branch (`master`; some tools call it `main`) intentionally contains
only this file. **It is not a runnable release. Do not develop or flash from it,
and never merge its file-removal commit into a version branch.** Existing code
and history live on the version branches.

## Versioning

- Each version has its own `funnypilot-X.Y.Z` branch. Historical suffixes such as
  `a`, `b`, `e` and `st` denote separate cuts, not interchangeable aliases.
  Compare numeric components (3.7.10 > 3.7.2); a suffixed cut follows its bare
  version. Newest does not mean vehicle-tested or appropriate for every task.
- Current development version: **`funnypilot-3.7.11`**, release commit
  `f7e393810`, based on clean 3.7.10 `71d40983c`. Fixes the force-triggered
  zero-assistance latch confirmed in steering-wander reports, sign handling of
  unwind friction credit, and the steering-pressure veto on stable curved
  post-signal handover. Enables bounded video/data exports during recording.
  All 287 targeted host regressions pass; vehicle validation remains pending.
  Read `docs/release-3.7.11.md` for evidence and limits. GitHub source release
  only; pushing does not install this version on a device.
- The user's requested version takes precedence over this pointer. If already
  working on a version branch, do not switch away or discard changes just to
  follow the newest branch. For device diagnosis, match its actual branch/hash;
  do not assume a GitHub push changed the device.

## Locate and read the correct instructions

1. Inspect `git status --short --branch` and `git remote -v`. Preserve local work.
   The fork is **https://github.com/jgray-dev/funnypilot.git**. Use a remote named
   `funnypilot`; if absent, add it:
   ```bash
   git remote add funnypilot https://github.com/jgray-dev/funnypilot.git
   ```
   Verify an existing remote points at this fork. `origin` may be the fork in a
   fresh clone or sunnypilot upstream in a developer checkout; do not guess.
2. Fetch and list available versions:
   ```bash
   git fetch funnypilot
   git branch --remotes --list 'funnypilot/funnypilot-*' --sort=-version:refname
   ```
3. Choose the requested branch, or the current-development branch above if none
   was specified. Read its guidance **before** editing:
   ```bash
   BRANCH=funnypilot-3.7.11  # replace if a different version was requested
   git show "funnypilot/$BRANCH:CLAUDE.md"
   ```
4. If no local branch of that name exists:
   ```bash
   git switch --track -c "$BRANCH" "funnypilot/$BRANCH"
   ```
   Otherwise use `git switch "$BRANCH"` and inspect its relationship to the
   fetched remote before updating. Never use a hard reset to solve a dirty tree.
   Read the checked-out **`CLAUDE.md` and `AGENTS.md`**, then follow those
   version-specific instructions. Consult its CHANGELOG/docs only as needed.

## Working and publishing

- Branch each new version from the selected code version, **not this branch**.
  Keep its branch name, `FUNNYPILOT_VERSION`, web diagnostic `EXPECTED_VERSION`
  and CHANGELOG consistent. Keep its CLAUDE.md concise; use docs/Git history for
  historical detail. Keep this default branch documentation-only.
- **Standing owner authorization: commit and push completed project work to the
  fork's remote Git host without asking permission again.** Git pushes do not
  deploy to the vehicle. Push the intended version branch to `funnypilot`, not
  upstream. This does not authorize unrelated history rewrites/branch deletion.
- **Flashing, restarting/rebooting, or changing vehicle/device settings still
  requires an explicit user request.** Never infer deployment permission from
  permission to push. Do not claim vehicle validation from local tests.
