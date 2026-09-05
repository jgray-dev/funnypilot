# Astra link protocol v1 (3.7.3e)

Implementation contract. HTTPS fixed origin `https://astra.jgray.cc`; no redirects. JSON UTF-8, maximum wire body 65536 bytes. All credentials are random 32-byte lowercase hex, bearer headers except pairing. No token logging. Poll idle 15s, active 2s, backoff with jitter. One dispatched request per device; up to 16 unfinished requests, 64 retained requests/device, results expire after 1h. Raw file/output chunks <=32768 bytes (base64 for file bytes); listings <=200 entries.

## Identity and safety
`identity`: `{branch: string|null, commit: string|null, dirty: boolean|null, version: string|null}` plus optional observation metadata. Dirty includes tracked/staged/untracked (`git status --porcelain --untracked-files=normal`, bounded); errors null. Approval compares ONLY these four fields, not timestamps. `state`: `{mode: 'offroad'|'onroad'|'unknown', reason: string}`; offroad requires fresh valid/seen deviceState and known nonempty pandaStates ignition off. Device enforces independently throughout commands. Network is not responsible for refreshing native safety state.

## Device endpoints
- `POST /device/pair` unauthenticated: `{pairingSecret, token}` where token is generated and retained privately by client BEFORE exchange; response `{deviceId}`. Pairing is one-time, 5min; repeat redemption by the SAME token is idempotent until expiry. Different token denied.
- `POST /device/poll` bearer device token: `{protocol:1,generation,bootId,identity,state}`. generation is random UUID per client start/reconnect uncertainty, not IP. Response `{pollAfter: 2|15, request: null|Request}`. Freshness server receipt time; online <45s. A changed generation invalidates dispatched reads and all approved/claimed/executing commands; executing becomes unconfirmed, never replay. Native state remains independent of HTTP timeouts.
- `POST /device/result`: `{id,generation,result}`. Accept only request bound to this device/generation and dispatched/executing or idempotent same result. Response `{ok:true}`. Explicit result `{ok:boolean,error?:string,...}`. Server infers completed/failed from result; unconfirmed work is never rerun.
- `POST /device/grant`: `{id,generation,digest}` after device durably records a command receipt while offroad. Atomically claimed -> executing; return `{id,digest,generation,leaseSeconds:5}` ONCE. Lost grant response means unconfirmed; repeat cannot obtain another grant. Grant receipt must be fresh: local request round trip <=2s and spawn within 2 monotonic seconds, with fresh safety/identity checks.
- `POST /device/lease`: `{id,generation,identity,state}` with fresh local identity/safety; refreshes presence and returns `{leaseSeconds:5}` only executing, not revoked, same generation, approval identity/state still valid and within execution deadline. Device renews about every 2s; failure/expiry cancels command. Lease cannot authorize starting another process.

`Request`: `{id,kind,args,digest,generation,bootId,identity,expires,status}`. kind `identity|list_files|read_file|tail_log|command`. `identity` is approval identity (four fields). `digest` SHA256 of canonical JSON `{kind,args}` (sorted object keys recursively, UTF-8, no whitespace, Unicode unescaped). Supported args:
- identity `{}`
- list_files `{path,limit?:1..200,offset?:0..10000}` (bounded nonrecursive page; truncation explicit)
- read_file `{path,offset?:nonnegative,length?:1..32768,allowRecording?:boolean}`
- tail_log `{path,length?:1..32768,allowRecording?:boolean}`
- command `{argv:string[],cwd:string,timeout:1..30}`; argv[0] absolute executable, no implicit shell, no sudo. Full argv displayed to owner. Environment fixed by device, not caller.

Files: roots `/data/openpilot`, `/data/log`, `/data/community/crashes`, `/data/funnypilot_triage`; `/data/media/0/realdata` and `/data/funnypilot_feedback` selected reads require allowRecording. Deny hidden/credential files, .git, .ssh, cloud.json, link config, private keys, traversal, symlinks and special files. Large reads (>4096 bytes), all recording reads, and commands require offroad; small diagnostics allowed onroad. No arbitrary decompression or environment/Params dumps. Each result carries path/device source, offsets, truncation/change metadata; base64 raw file data. Known secret redaction cannot guarantee arbitrary files contain no secrets; arbitrary explicitly approved programs are NOT sandboxed.

## Browser owner routes (existing cookie/Origin/CSRF)
- GET `/api/linked-devices` -> `{devices:[DeviceSummary]}`; summary `{id,name,lastSeen,online,generation,bootId,identity,state,revoked}`. Never credential/hash.
- POST `/api/linked-devices/pair` `{name}` -> `{pairingSecret,expires}`.
- POST `/api/linked-devices/revoke` `{deviceId}` -> `{ok:true}`.
- GET `/api/linked-devices/requests?deviceId=...&offset=0` -> `{requests:[RequestSummary],nextOffset:null|number,truncated:boolean}` (exact argv/cwd/digest/identity/status/result/expiry; no credential). Byte-budgeted pages never truncate approval arguments; offsets are bounded to64 and may shift as new requests arrive. Tool result envelopes are also byte-budgeted; generic command output may be further shortened with `truncated:true` to retain full argv/provenance.
- POST `/api/linked-devices/approve` `{id,digest}` -> `{ok:true}`. Only pending commands; device freshly online/offroad and unchanged generation/identity; binds approver browser ID, 120s expiry. Reject approval on browser revocation/expiry at grant/dispatch. Approval is not native permission-menu input.
- POST `/api/linked-devices/reject` `{id}` -> `{ok:true}`; cancels pending/approved requests, executing -> cancelled (next lease denied). Approval/reject unavailable to session or device credentials.

## Host/harness integration
- GET `/bridge/device-session?nativeId=UUID` (host bridge bearer ONLY) -> `{binding:null|{nativeId,deviceId,cwd}}`. Native binding survives capability expiry and broker metadata loss.
- POST `/bridge/device-session` (host bridge bearer ONLY) `{session,nativeId,cwd,deviceId,token}`. Register hashed session capability; bind immutable nativeId/deviceId/cwd `/home/astro/Development/c3x`. Existing native binding cannot be changed. Re-register same session token idempotently. Session capabilities expire after 24h; revoke on device revoke.
- GET `/tool/status` (session capability bearer) -> `{device:DeviceSummary,session}`.
- POST `/tool/request` `{kind,args}` -> `{request:RequestSummary}`. Target/session taken from capability, never caller. Reads queued; commands pending human approval. No automatic retry of commands; tool returns ID immediately. Identity request result supplies a fresh uncached probe.
- POST `/tool/result` `{id}` -> `{request:RequestSummary}` scoped to capability's session.

Host launch preserves model/provider, original MCP settings, adds native `--mcp-config` stdio adapter and `--append-system-prompt`. MCP env contains only scoped session credential and origin, never bridge secret. Tools device_status, compare_checkout, list_files, read_file, tail_log, request_command, command_result. Read tools may poll result up to 35s; explicit offline/timeout result never local fallback. compare_checkout compares returned remote identity to fresh local git in fixed cwd. Preserve device binding across raw native resume/continue and browser-created wrapper session IDs; never automatically switch local branches.

## Safety/retention
Device receipt journal is 0600 outside checkout, written only confirmed offroad before generic execution. Corrupt/unwritable/full journal disables commands. No executing/claimed command may be replayed following lost acknowledgment, restart, generation change or expired grant. Server states pending -> approved -> claimed -> executing -> completed/failed, plus queued/dispatched reads and rejected/cancelled/expired/unconfirmed. Dispatched reads can be retried under a new request ID; commands require a new owner approval. Store final result acknowledgment receipts in memory onroad; do not introduce durable control-loop IO. No live deployment or pairing is implied by implementation.
