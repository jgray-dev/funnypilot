# Drive harvesting for model training (3.7.13)

Goal: keep every drive from this car in private cloud storage so models can be
trained on it later. 3.7.13 is source-only: the Worker and bucket are live, the
device code is **not installed and not provisioned**. No driving behavior changes.

## What is harvested

Per one-minute loggerd segment: `fcamera.hevc` (road), `ecamera.hevc` (wide road),
`rlog.zst` (full-rate log: poses, CAN, controls, model outputs), `qlog.zst`,
`qcamera.ts`. Cabin camera (`dcamera.hevc`) and audio are never listed, hashed or
accepted by the Worker (its artifact allow-list rejects them server-side too).

Road video contains other people's faces and plates. The bucket is private, the
device credential is upload-only, and nothing here makes anything public.

## Pieces

| Piece | Where |
| --- | --- |
| Device daemon | `sunnypilot/harvest/harvestd.py` (process `funnypilot_harvest`) |
| Segment scan + `user.fp_harvested` mark | `sunnypilot/harvest/segments.py` |
| Checksummed multipart upload | `sunnypilot/harvest/uploader.py` |
| Deleter policy | `sunnypilot/harvest/retention.py`, hooks in `system/loggerd/deleter.py` |
| Worker (R2 + D1) | `cloud/drives` → `https://funnypilot-drives.nohaxjustdoge.workers.dev` |
| Training-side CLI | `tools/drives_cloud.py` (`stats`, `list`, `download`) |

Cloudflare account `831514824c4a5fba9c759e88c09ff40c`: R2 `funnypilot-drives`,
D1 `funnypilot-drives` (`bc0ad43c-2294-4620-93fb-ffed2086d0b3`), Worker secret
`UPLOAD_TOKEN`. The token was generated on the dev machine at
`~/.config/funnypilot/drives_upload_token` (mode 600). It is not in git.

Protocol: `PUT /segments/<route>/<n>` (manifest) → `PUT …/files/<name>/<i>` (16 MiB
parts, R2 verifies SHA-256 while streaming) → `POST …/complete` (Worker lists the
prefix once and checks every part). A lost response is safely retried; a complete
segment accepts only an identical manifest.

## Device behavior

- Idle (zero work) until `/data/funnypilot_harvest/cloud.json` exists. The file is
  `{"endpoint": "https://funnypilot-drives.nohaxjustdoge.workers.dev", "token": "…"}`.
- Uploads only when `IsOnroad` is false and the hardware network type is Wi-Fi,
  re-checked between every request; stops within one request of ignition.
- `nice 19`, off the control cores (`configure_background_thread`), no msgq
  subscription, so the reader-budget rule is untouched (census test passes).
- Segments are skipped while a `.lock` exists or files changed in the last 30 s.
- Five consecutive failures park that segment for an hour so one bad segment cannot
  starve the rest. Status (counts, last error *type*) is in `/dev/shm/fp_harvest_status.json`.
- Retention: while linked, a route with an unharvested segment newer than 30 days is
  not expired at 7 days. Under free-space pressure the deleter removes harvested
  segments first, then the rest. **Pressure always deletes**; a device that never sees
  Wi-Fi loses oldest data first, exactly as before.

## Provisioning (needs your explicit go-ahead; touches the device)

```bash
# from the dev machine; the token never appears in a command line or log
python3 - <<'PY' | ssh comma@192.168.86.31 'umask 077; mkdir -p /data/funnypilot_harvest; cat > /data/funnypilot_harvest/cloud.json'
import json, pathlib
print(json.dumps({"endpoint": "https://funnypilot-drives.nohaxjustdoge.workers.dev",
                  "token": pathlib.Path.home().joinpath(".config/funnypilot/drives_upload_token").read_text().strip()}))
PY
```

Then install 3.7.13 on the device (separate request), verify version **and commit**,
manager health, and that `funnypilot_harvest` is running. Check
`cat /dev/shm/fp_harvest_status.json` after the first parked Wi-Fi session, and
`python tools/drives_cloud.py stats` on the dev machine.

## Verification so far (and its limits)

- 11 host tests (`sunnypilot/harvest/tests`): segment eligibility, cabin/audio
  exclusion, part sizes and byte-for-byte reassembly, a file changed after hashing is
  never marked, gate closing mid-upload never completes, `|` in route names,
  backoff/poison-segment skipping, expiry grace and pressure ordering.
- Python uploader ↔ the real Worker code under `wrangler dev` with local R2/D1:
  upload completed, D1 row `complete`, 25 MiB stored (cabin file excluded), wrong
  token rejected. Production: `/health`, 401 without token, `stats` and `list` work.
- Not verified: anything on the vehicle or device. The device was unreachable
  during this session, so real segment sizes, upload throughput, deleter behavior on
  the device filesystem (xattr support) and `Params`/hardware gating are unmeasured.

## Cost and size (estimates; measure on the device)

Road + wide video are 10 Mbps each at C3X resolution, about 150 MB/min together plus
tens of MB of logs: **roughly 10 GB per hour of driving**. R2 is $0.015/GB-month with
**no egress fees**: about $0.15 per driving hour per month stored, so 200 hours ≈ 2 TB ≈
$30/month. Upload time at home is roughly 1:1 with drive time on a 25 Mbps uplink.
The first 10 GB/month of R2 storage is free. Uploading only happens while the
device is powered and parked on Wi-Fi, so how long the car keeps the C3X powered after
ignition-off sets your throughput ceiling.

## Machine learning: realistic path

Be honest about the ladder; each rung is a separate decision.

1. **Collect (this release).** Cheap and irreversible: drives you do not record now
   are gone. Keep engaged *and* manual segments; manual driving is the most valuable
   "what a human does" signal, and `rlog` records which is which.
2. **Learn from `rlog` only (CPU, no GPU).** Personal following-distance/accel
   preference, lateral torque/friction/delay identification, brake-onset timing.
   These feed the controllers FunnyPilot actually owns (MPC, torque, SCC) and are the
   fastest route to something measurable on the car.
3. **Fine-tune or distill a driving model (GPU).** Video + logs give self-supervised
   targets (where the car actually went, from localizer poses). One driver's data is
   far too small to train a driving model from scratch; it can adapt an existing model
   and cover this car's camera/lens. Expect a personal PC GPU to be enough for
   experiments; rent an EC2 GPU (e.g. a spot g5/g6) only for runs that outgrow it. R2
   has no egress charge, but EC2 pulling terabytes still costs time; sync once.
4. **Run it on the C3X.** This is the hard part. The device's tinygrad/modeld path is
   the known blocker: in this fork, non-stock model bundles have hung on the tici
   (see memory note "modeld_tinygrad broken on tizi"). Do not train toward a target you
   cannot execute. Prove an arbitrary bundle runs on the device **before** spending on
   training.

Risks: a model trained on one driver inherits their habits and mistakes; closed-loop
behavior can differ from open-loop validation; and anything that steers a car needs
the same vehicle-validation discipline as the controller changes in this repo.

Bulk sync for training (recommended over `drives_cloud.py download`, which is one
object per call): create an R2 S3 API token in the Cloudflare dashboard (R2 → Manage
API tokens, read-only on `funnypilot-drives`) and `rclone sync` the `drives/` prefix
to local disk. Wrangler cannot create that token.

## Open questions for the owner

- Which GPU does the personal PC have? (decides whether EC2 is ever needed)
- Does the car keep the C3X powered after ignition-off, and for how long?
- Should `qlog`/`qcamera` (small, redundant for training) be dropped to save space?
