# FunnyPilot 3.7.3e — Astra-linked Comma 3X

## Web deployed; broker and device activation pending

Based on `funnypilot-3.7.2` commit
`5ec74a3c776c2cdcf1d1c341e1017966051862c1`. This release adds an optional
outbound device client and extends the existing Astra website/harness. No
controller tuning, vehicle safety-gate bypass, native schema, Params key, or
on-device LLM was added.

The owner explicitly authorized web deployment on 2026-09-05. Website and Worker
are live at **https://astra.jgray.cc**, version
`efa1c9e1-4f2b-4588-b718-1eff88124c28`, deployed at **23:41:38.320 UTC** with 100%
traffic. Published HTML/JS/CSS match the build byte-for-byte; unauthenticated
`/api/linked-devices` and `/api/state` return401. Verified at23:42:29 UTC.

**No host broker restart, device flash/restart, device configuration change,
real pairing, paid model call or live driving validation was performed.**
Linked hardware is visible after refresh, but the old running brokers must be
activated separately before linked conversations work. A tested capability
preflight returns503 `BROKER_ACTIVATION_REQUIRED` rather than silently dropping
the selected device. Conservatively, all conversation resumes are blocked until
both brokers are updated; already-running and new ordinary conversations remain
available.

## Architecture discovered on this host

The website source is `/home/astro/sandbox/astra-web`, not the empty
`~/Development/astronaut` directory. It is currently outside Git. The public
`astra.jgray.cc` endpoint is an existing Cloudflare Worker (`astra-control`) with
an owner-scoped SQLite Durable Object (`OwnerControl`, binding `CONTROL`). This
host connects **outbound** through the existing PTY and chat brokers. There is
no need for an incoming vehicle port, fixed hotspot IP, VPN, SSH tunnel, new
Cloudflare storage product, or reverse proxy to the vehicle's port 8888.

The same architecture now accepts a lightweight Comma HTTPS poller. Polls carry
hardware identity/state and request work; results return through authenticated
POSTs. Initial tunables: 15s idle, 2s while a device is being viewed or has work;
short bounded retries/backoff on failure. CPU, bandwidth and mobile-network
latency have not been measured on the actual Comma. Polling is not zero-cost:
existing Worker/DO request and storage billing still applies.

The host keeps the existing `/home/astro/.local/bin/astra` launcher, native Claude
Code harness, and `gpt-6-astra` provider. No new model API or gateway migration.
Host additions use the official MCP SDK and Zod; device networking uses existing
Python dependencies. Development installation retained the existing locked
Astra dependency versions and did not run lifecycle scripts or restart services.

## Using the linked session after authorized activation

1. Sign in to Astra normally; **Linked hardware** is distinct from remembered
   **browser devices**. Create a one-time pairing secret, revealed only locally
   in the authenticated panel. It expires in five minutes.
2. On the installed, ignition-off Comma, enter the secret through the local
   hidden prompt (use the device's working Python environment):
   `python /data/openpilot/tools/astra_link.py pair`.
   The pairing command must have fresh valid native ignition/offroad evidence;
   it is not meant for the development host or a running vehicle.
3. After the optional daemon notices its config (up to about 30 seconds while
   disabled), the device becomes visible. Select it when starting a new Astra
   conversation. LOCAL cwd is `/home/astro/Development/c3x`; REMOTE paths refer
   to the Comma, usually `/data/openpilot` and `/data/log`.
4. Ask Astra to compare checkouts, list/read logs or inspect device files. The
   MCP toolset includes `device_status`, `compare_checkout`, `list_files`,
   `read_file`, `tail_log`, `request_command`, and `command_result`.
5. Generic commands appear as immutable proposals in Linked hardware. Review
   the exact executable/argv, cwd, timeout, device/session, boot/generation,
   identity and digest before approving. Native terminal permission menus and
   permission-bypass mode are **not** command authorization. Reject/cancel and
   device revocation are separate explicit owner actions.

Device binding survives browser resume and native-ID wrapper changes. A bound
native conversation cannot silently become unlinked or target another device.
Ambiguous native `--continue`, resume pickers and forks are refused with guidance
to use an explicit `--resume UUID`; this is a deliberate broker limitation. Explicit native
resume also needs the remote binding registry available, rather than guessing an
unlinked target during a network outage. Already running sessions are unaffected.
A 24h tool capability expiry requires a new/resumed authorized session. Do not
expect an old continuously running session's credential to renew itself.

The configuration at `/data/astra_link/config.json` uses a private 0700 directory
and 0600 file. `status` prints only paired/enabled flags. `enable`/`disable` require
fresh offroad state and do not restart the manager. Credential replacement after
revocation is explicit local maintenance, not automatic re-pairing. Never print,
commit, upload or paste this config or feedback `cloud.json` into a conversation.

## Authorization, data and resource boundaries

- Separate per-hardware token, per-session tool capability, owner browser cookie,
  and host relay secret. The vehicle/tool tokens cannot sign in a browser, replace
  the host relay, approve commands, or access another device/session's requests.
- Owner mutation routes reuse exact Origin and CSRF checks. Hashed hardware and
  capability credentials, short single-use pairing, durable request transitions,
  bounded queue/retention, explicit revocation, no credentials in URLs.
- Device operations require descriptor-relative no-follow regular-file access;
  no path traversal, symlink escape, device/FIFO reads, hidden files, private keys,
  credential configs, arbitrary Params or process-memory reads.
- Default roots: `/data/openpilot`, `/data/log`, `/data/community/crashes`,
  `/data/funnypilot_triage`. Explicit selected recording/feedback chunks can use
  `/data/media/0/realdata` or `/data/funnypilot_feedback` with `allowRecording`;
  recording directory enumeration, automatic syncing and decompression are not
  supported. Known secrets are redacted, but arbitrary logs cannot be guaranteed
  secret-free. Redacted bytes are marked, not claimed to be original file bytes.
- Default file reads are 4096 bytes. Larger reads and selected recordings require
  offroad. Maximum raw chunk/command output is 32768 bytes; listings scan bounded
  pages of at most 200 entries. Request/result JSON is bounded; extra escaped
  command output may be shortened to preserve exact argv/provenance. Browser
  history is byte-budgeted with Older/Newest pages, not unbounded result dumps.
- Up to 16 unfinished requests/device and 64 retained records. Result content is
  deleted after one hour by an idle alarm (cleanup can lag by the alarm interval),
  not retained forever if the device disconnects. Pairing/hardware/session counts
  are bounded; durable native bindings remain to prevent resume rebinding.
- All generic commands require valid, recently received `deviceState` and known,
  nonempty `pandaStates`, with ignition off. Freshness is computed from consumer
  monotonic `sm.recv_time`, not epoch time. Disengaged, startup-blocked,
  `started=false` alone, or an unknown/disconnected panda is not parked proof.
- The independent native monitor continues during HTTP/command execution.
  Commands use exact argv with no implicit shell, no sudo, fixed environment,
  closed stdin, bounded output/time, process-group termination and a 5s lease.
  A lost lease, ignition/state/identity change or revocation cancels eligible work.
- An offroad-only durable receipt precedes the fresh server execution grant.
  Duplicate/expired grants cannot restart a command. Lost grants/results and
  interrupted execution are not automatically replayed. Receipt storage is capped
  at 256 entries and fails closed if corrupt/full/unwritable; resetting it requires
  explicit local maintenance after revocation/settling uncertain work.
- Generic owner-approved programs are **privileged maintenance, not sandboxed**.
  They can access secrets, modify files or escape ordinary process groups. Limits
  and redaction are not an adversarial sandbox, cannot undo side effects, and do
  not defend against a compromised trusted host with existing Bash/SSH access.
- Cloudflare terminates TLS and stores selected request/results temporarily.
  This is private authenticated transport, not end-to-end encryption excluding
  Cloudflare. Native Astra transcripts can retain selected tool output under the
  host's existing conversation retention policy.

The optional `funnypilot_astra` process is manager-supervised and excluded only
from the driving required-process gate, alongside the existing optional entries.
Failure does not disable driving; required processes and camera/calibration
checks remain. The client imports inertly, runs at lower priority, and does not
write application-owned durable `/data` state onroad. It does not keep the vehicle
awake or change feedback's parked-Wi-Fi upload policy. A manager-supervised client
is **not** a rescue service when manager cannot start at all.

## Code/log provenance

A linked session receives native `--mcp-config` and `--append-system-prompt`
configuration without replacing existing MCP settings or changing the global
Astra profile. `compare_checkout` runs an uncached remote identity operation and
read-only local Git probes with optional locks disabled. It reports actual branch,
full commit and dirty/unknown state; untracked files use Git's `normal` policy.
No automatic checkout, reset, pull, deploy or overwrite follows a mismatch.

Two dirty checkouts are not established equivalent. Missing/failed probes are
unknown, not clean. Device identity includes startup disk observation separately
from the current disk probe; neither proves the running controllers loaded those
same files. **An older drive requires its recording-time identity**, not today's
checkout. The host reports receipt timing/probe round trip without pretending
independent host/device clocks provide an exact identity age.

## Source preservation and activation boundaries

Inspected original Astra source was backed up privately on this host under
`~/.local/state/astra-web-source-backups/3.7.3e-20260905T225603Z` before edits.
`docs/astra-web-3.7.3e.patch` contains only changed/new source, tests and dependency
manifests; `docs/astra-web-3.7.3e-manifest.json` records before/after SHA-256 hashes.
No live credentials/config, recordings, conversations, build output or dependency
installation tree is included. The patch was applied against the source backup
in a temporary directory and all resulting source hashes checked.

Do not apply the patch blindly to already edited/live source. Check its baseline
hashes first. The local non-Git source already contains these edits; applying the
patch there again is unnecessary.

Web deployment used `npm run check`, `npm run build`, Wrangler dry-run, then
`wrangler deploy --keep-vars` with existing account authentication. No credential
rotation or live configuration edits were performed.

**Remaining activation was not performed** and requires an explicitly authorized window:
- Check running Astra sessions before restarting `astra-web`/`astra-chat`.
  **Restarting `astra-web` terminates its PTYs**, including active agent sessions.
  Restarting just chat cannot load new PTY-broker creation code.
- Install/flash the requested vehicle release only with explicit owner approval.
  Verify version **and full commit**, manager/UI health and port8888 per `CLAUDE.md`.
- Pair while safely ignition-off and verify small diagnostics before a harmless,
  explicitly approved command. Actual hotspot reconnection, ignition transitions,
  native service freshness and target-runtime imports still need device validation.

## Verification

Local checks completed on this host:
- **185 Python tests**, including the actual device client/runner, fault injection,
  feedback/process-health regressions, diagnostic markers and capnp annotation
  guard. **175 diagnostic markers resolve.** Changed Python passes Ruff.
- **73 Astra tests**, including fail-closed old-broker capability checks,
  real SQLite transitions with injected clocks,
  browser UI fixtures, scoped native MCP subprocesses, existing auth/relay tests,
  and end-to-end **MCP → local Worker → real Python device/operations**.
- TypeScript check, broker syntax checks and temporary-output production build.
  Vite reports its existing-style large-bundle warning; no claim of bundle-size
  optimization. Node SQLite/type-transform test APIs emit experimental warnings.
- Guard removals/restored bugs were caught for device receipt replay, native
  registry binding/fixed cwd, unknown local Git status, ungranted command success,
  and resurrected denied leases. Initial outdated diagnostic-comment marker and
  line-length failures were fixed and the checks rerun successfully.

Reproduce Python checks from the c3x root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /tmp/fp-audit-venv/bin/python -m pytest \
  --noconftest -q -p no:cacheprovider -o addopts='' \
  sunnypilot/astra_link/tests sunnypilot/feedback/tests \
  sunnypilot/navd/tests/test_diagnostic_markers.py \
  sunnypilot/tests/test_capnp_annotations.py
uv tool run ruff check --no-cache sunnypilot/astra_link tools/astra_link.py \
  sunnypilot/feedback/tests/test_process_health.py selfdrive/selfdrived/selfdrived.py \
  system/manager/process_config.py sunnypilot/navd/nav_webserver.py
```

Astra's `node scripts/test-device-link.mjs` uses isolated Wrangler config/storage,
synthetic credentials and temporary build output, not live `.dev.vars`, live
services or `dist`. `ASTRA_TEST_PYTHON` can select a compatible Python environment;
the Python fixture imports this host's c3x checkout. Browser tests use local
Chromium. No test invokes a paid model or connects to the real vehicle.

These are synthetic software/protocol checks, **not** native-device, public-site,
closed-loop control or vehicle-safety validation. Installed CLI flags and MCP
protocol behavior were checked, but a live model obeying resumed-session
instructions was not exercised.

## References

- [Native MCP configuration](https://code.claude.com/docs/en/mcp)
- [Native CLI flags](https://code.claude.com/docs/en/cli-reference)
- [Official MCP TypeScript SDK](https://ts.sdk.modelcontextprotocol.io/server)
- [Durable Object lifecycle](https://developers.cloudflare.com/durable-objects/concepts/durable-object-lifecycle/)
- [SQLite storage transactions](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
- [Wire contract](astra-link-protocol.md)
