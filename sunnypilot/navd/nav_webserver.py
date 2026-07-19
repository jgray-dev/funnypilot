"""
FunnyPilot terminal server — PTY-backed bash shell over WebSocket at /ws.
Branch selector sorted by last commit date (most recent first).
POST /api/flash  — hard-sets device to chosen funnypilot branch and reboots.
GET  /api/branches — list funnypilot branches, newest first.
"""
import asyncio
import hashlib
import json
import os
import pty
import fcntl
import re
import termios
import time
import struct
import aiohttp
from aiohttp import web

REPO = "jgray-dev/funnypilot"
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "nav_web")
_SHELL = "/bin/bash"
_PORT = 8888

# FunnyPilot v3.2.7: triage flight-recorder logs (see
# selfdrive/controls/lib/triage_recorder.py for the format and rationale).
TRIAGE_DIR = "/data/funnypilot_triage"
_TRIAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(jsonl|jsonl\.1|log)$")
_PULSE_PERIOD_S = 600  # code-identity pulse every 10 min, catches mid-parked swaps
_PULSE_MAX_BYTES = 1024 * 1024
# files whose on-disk content defines the "smoothing" feel — hashed each pulse
_FEEL_FILES = [
  "/data/openpilot/selfdrive/controls/lib/lat_smooth.py",
  "/data/openpilot/selfdrive/controls/lib/eps_limit.py",
  "/data/openpilot/selfdrive/controls/lib/long_shaping.py",
  "/data/openpilot/selfdrive/controls/controlsd.py",
  "/data/openpilot/selfdrive/controls/lib/latcontrol_torque.py",
]

# Expected version for the running branch (used by /api/diagnostics).
EXPECTED_VERSION = "3.3.8"

# FunnyPilot v3.3.3: the Verify list is CONSOLIDATED — one row per question
# the user actually asks ("is my code intact / will it stay that way"),
# instead of one row per historical grep. All commands are non-mutating.
# `-c safe.directory=*` avoids git "dubious ownership" failures when the
# webserver uid differs from the checkout owner.
_GIT = "git -c safe.directory='*' -C /data/openpilot"

# Every load-bearing FunnyPilot marker, verified in ONE check. Each entry:
# (grep pattern, file, short label). A missing marker means the on-disk code
# is not the shipped branch — the single "code" row fails and names it.
_CODE_MARKERS = [
  ("class LatSmoother", "/data/openpilot/selfdrive/controls/lib/lat_smooth.py", "lat knot smoother"),
  ("SPLINE", "/data/openpilot/selfdrive/controls/lib/lat_smooth.py", "C1 spline shaping"),
  ("class EpsTorqueGovernor", "/data/openpilot/selfdrive/controls/lib/eps_limit.py", "EPS torque governor"),
  ("v3.3.8", "/data/openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py", "MPC blended mode restore"),
  ("v3.3.6", "/data/openpilot/selfdrive/controls/controlsd.py", "controlsd smoother wiring"),
  ("v3.3.2", "/data/openpilot/sunnypilot/modeld_v2/modeld.py", "EMA smoothing revert"),
  ("_OVERRIDE_MIN_SCALE", "/data/openpilot/selfdrive/controls/lib/latcontrol_torque.py", "override softening"),
  ("class OverrideGate", "/data/openpilot/selfdrive/controls/lib/override_gate.py", "override gate"),
  ("v3.2.3st", "/data/openpilot/opendbc_repo/opendbc/car/hyundai/carcontroller.py", "brake chime fix"),
  ("UNWIND_SETTLE_TIME", "/data/openpilot/sunnypilot/selfdrive/controls/lib/blinker_pause_lateral.py", "blinker unwind"),
  ("class AccelJerkShaper", "/data/openpilot/selfdrive/controls/lib/long_shaping.py", "long output shaper"),
  ("v3.3.3", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "long planner + SLA gas gate"),
  ("class CurveSpeedCap", "/data/openpilot/sunnypilot/selfdrive/controls/lib/long_v2/curve_cap.py", "SCC v2 curve cap"),
  ("v3.3.3", "/data/openpilot/sunnypilot/selfdrive/controls/lib/speed_limit/speed_limit_assist.py", "SLA arrow activation"),
  ("v3.3.0e", "/data/openpilot/opendbc_repo/opendbc/car/hyundai/values.py", "K5 radar tracks flag"),
  ("radar_enable.jsonl", "/data/openpilot/opendbc_repo/opendbc/sunnypilot/car/hyundai/enable_radar_tracks.py", "verified radar enable"),
  ("FINALIZED_BRANCH", "/data/openpilot/launch_chffrplus.sh", "boot branch guard"),
  ("adopting flashed branch", "/data/openpilot/system/updated/updated.py", "updater self-heal"),
  ("class TriageRecorder", "/data/openpilot/selfdrive/controls/lib/triage_recorder.py", "triage recorder"),
  ("HIDDEN_CRUISE_OFFSET", "/data/openpilot/selfdrive/controls/lib/longitudinal_planner.py", "hidden cruise governor"),
  ("class LagdElement", "/data/openpilot/selfdrive/ui/sunnypilot/onroad/developer_ui/elements.py", "lagd dev UI readout"),
  ("class AutoUpdater", "/data/openpilot/sunnypilot/auto_updater/manager.py", "offroad wifi auto-updater"),
]
_CODE_CMD = "; ".join(
  f"grep -qs '{pat}' '{path}' && echo 'ok       {label}' || echo 'MISSING  {label}'"
  for pat, path, label in _CODE_MARKERS
)

# Updater state in one row: the target branch (what a background fetch would
# stage) and any already-staged update. Mismatches here are the root cause of
# the historical "reverts after sitting parked" issue.
_UPDATER_CMD = (
  "echo \"target: $(cat /data/params/d/UpdaterTargetBranch 2>/dev/null || echo '(unset)')\"; " +
  "echo \"staged: $(git -c safe.directory='*' -C /data/safe_staging/finalized rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(none)')\""
)

DIAG_CHECKS = [
  {"id": "version", "name": "FunnyPilot version",       "cmd": "cat /data/openpilot/FUNNYPILOT_VERSION 2>&1"},
  {"id": "branch",  "name": "Git branch",               "cmd": f"{_GIT} rev-parse --abbrev-ref HEAD 2>&1"},
  {"id": "clean",   "name": "Working tree unmodified",  "cmd": f"{_GIT} status --porcelain 2>&1"},
  {"id": "code",    "name": "Shipped code markers",     "cmd": _CODE_CMD},
  {"id": "updater", "name": "Updater target / staged",  "cmd": _UPDATER_CMD},
  {"id": "model_bundle", "name": "Active model bundle", "cmd": "cat /data/params/d/ModelManager_ActiveBundle 2>/dev/null || echo '(none / stock)'"},
  {"id": "logs",    "name": "Triage logs on disk",      "cmd": "ls -sh1 /data/funnypilot_triage/ 2>/dev/null || echo '(no logs yet)'"},
]


def _eval_diag(check_id: str, out: str):
  """Return (level, summary) for a check. level in pass|fail|warn|info."""
  s = out.strip()
  if check_id == "version":
    return ("pass", s) if s == EXPECTED_VERSION else ("warn", f"{s or '(empty)'} (expected {EXPECTED_VERSION})")
  if check_id == "branch":
    return ("pass" if EXPECTED_VERSION in s else "warn", s or "(unknown)")
  if check_id == "clean":
    return ("pass", "clean") if s == "" else ("fail", "MODIFIED")
  if check_id == "code":
    missing = [ln.split(None, 1)[1] for ln in s.splitlines() if ln.startswith("MISSING")]
    if missing:
      return ("fail", f"MISSING: {', '.join(missing)}")
    return ("pass", f"all {len(_CODE_MARKERS)} markers present")
  if check_id == "updater":
    # A target that differs from the flashed branch is exactly what caused the
    # "reverts after sitting offroad" issue — the updater stages that branch.
    target = staged = ""
    for ln in s.splitlines():
      if ln.startswith("target:"):
        target = ln[len("target:"):].strip()
      elif ln.startswith("staged:"):
        staged = ln[len("staged:"):].strip()
    if target not in ("", "(unset)") and EXPECTED_VERSION not in target:
      return ("fail", f"updater targets a DIFFERENT branch: {target}")
    if staged not in ("", "(none)") and EXPECTED_VERSION not in staged:
      return ("warn", f"staged: {staged} (boot guard will discard it)")
    return ("pass", f"target {target or '(unset)'}, staged {staged or '(none)'}")
  return ("info", "")


async def _run_diag_check(check: dict) -> dict:
  try:
    proc = await asyncio.create_subprocess_shell(
      check["cmd"],
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    text = out_b.decode(errors="replace")
  except asyncio.TimeoutError:
    text = "(timed out)"
  except Exception as e:
    text = f"(error: {e})"
  level, summary = _eval_diag(check["id"], text)
  return {"id": check["id"], "name": check["name"], "cmd": check["cmd"],
          "output": text.rstrip(), "level": level, "summary": summary}


async def handle_diagnostics(request: web.Request) -> web.Response:
  try:
    results = await asyncio.gather(*[_run_diag_check(c) for c in DIAG_CHECKS])
    return web.json_response({"checks": list(results)})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def _fetch_branches(session: aiohttp.ClientSession):
  branches = []
  page = 1
  while True:
    url = f"https://api.github.com/repos/{REPO}/branches?per_page=100&page={page}"
    async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as resp:
      if resp.status != 200:
        break
      data = await resp.json()
      if not data:
        break
      branches.extend(data)
      if len(data) < 100:
        break
      page += 1

  # Filter to funnypilot-* branches, then fetch commit dates concurrently
  fp_branches = [b for b in branches if b["name"].startswith("funnypilot-")]

  async def get_date(branch):
    commit_url = branch["commit"]["url"]
    try:
      async with session.get(commit_url, headers={"Accept": "application/vnd.github+json"}) as r:
        if r.status == 200:
          c = await r.json()
          date = c.get("commit", {}).get("committer", {}).get("date", "")
          return branch["name"], date
    except Exception:
      pass
    return branch["name"], ""

  results = await asyncio.gather(*[get_date(b) for b in fp_branches])
  sorted_branches = sorted(results, key=lambda x: x[1], reverse=True)
  return [{"name": name, "date": date} for name, date in sorted_branches]


async def handle_branches(request: web.Request) -> web.Response:
  try:
    async with aiohttp.ClientSession() as session:
      branches = await _fetch_branches(session)
    return web.json_response(branches)
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_flash(request: web.Request) -> web.Response:
  try:
    body = await request.json()
    branch = body.get("branch", "").strip()
    if not branch:
      return web.json_response({"error": "missing branch"}, status=400)
    # Only allow funnypilot branches or sunnypilot main/dev/staging
    allowed = branch.startswith("funnypilot-") or branch in ("main", "dev", "staging")
    if not allowed:
      return web.json_response({"error": "disallowed branch"}, status=403)

    # Keep the stock updater aligned with the explicit flash: point its target
    # at the flashed branch so it can never stage (and boot-swap in) a stale
    # branch after an offroad fetch. Best-effort — the launch_chffrplus.sh
    # branch guard and the updated.py self-heal are the real backstops.
    try:
      from openpilot.common.params import Params
      Params().put("UpdaterTargetBranch", branch)
    except Exception:
      pass

    # After a successful checkout, also discard any previously staged update
    # (unmount the updater overlay first) so the reboot below can't swap in
    # code that was finalized before this flash.
    script = (
      f"cd /data/openpilot && "
      f"sudo git -c http.sslVerify=false fetch funnypilot {branch} && "
      f"sudo git checkout {branch} && "
      f"sudo git reset --hard funnypilot/{branch} && "
      f"{{ sudo umount -l /data/safe_staging/merged 2>/dev/null; "
      f"sudo rm -rf /data/safe_staging; "
      f"sudo reboot; }}"
    )
    proc = await asyncio.create_subprocess_exec(
      "/bin/bash", "-c", script,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    asyncio.ensure_future(proc.wait())
    return web.json_response({"status": "flashing", "branch": branch})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  master_fd, slave_fd = pty.openpty()

  env = dict(os.environ)
  env.pop("PYTHONHOME", None)
  venv_path = env.pop("VIRTUAL_ENV", None)

  if venv_path:
    path_entries = [p for p in env.get("PATH", "").split(":") if p and not p.startswith(venv_path)]
    if path_entries:
      env["PATH"] = ":".join(path_entries)
    else:
      env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
  env.setdefault("HOME", os.environ.get("HOME", "/root"))
  env["TERM"] = "xterm-256color"

  proc = await asyncio.create_subprocess_exec(
    _SHELL, "--login",
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    cwd="/data/openpilot",
    close_fds=True,
    env=env,
  )
  os.close(slave_fd)

  if venv_path:
    try:
      os.write(master_fd, b"deactivate\n")
    except OSError:
      pass

  loop = asyncio.get_event_loop()

  async def pty_reader():
    try:
      while not ws.closed:
        try:
          data = await loop.run_in_executor(None, lambda: os.read(master_fd, 4096))
          if not data:
            break
          await ws.send_bytes(data)
        except OSError:
          break
    finally:
      if not ws.closed:
        await ws.close()

  reader_task = asyncio.ensure_future(pty_reader())

  try:
    async for msg in ws:
      if msg.type == aiohttp.WSMsgType.BINARY:
        data = msg.data
        if len(data) > 1 and data[0] == 0xFF:
          # Resize packet: 0xFF + 4 bytes (cols, rows as uint16 LE each)
          if len(data) >= 5:
            cols = data[1] | (data[2] << 8)
            rows = data[3] | (data[4] << 8)
            try:
              winsize = struct.pack("HHHH", rows, cols, 0, 0)
              fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
              pass
        else:
          try:
            os.write(master_fd, data)
          except OSError:
            break
      elif msg.type == aiohttp.WSMsgType.TEXT:
        try:
          os.write(master_fd, msg.data.encode())
        except OSError:
          break
      elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
        break
  finally:
    reader_task.cancel()
    try:
      proc.kill()
    except Exception:
      pass
    try:
      os.close(master_fd)
    except OSError:
      pass
    await proc.wait()

  return ws


# ---------------------------------------------------------------------------
# FunnyPilot v3.2.7 triage: code-identity snapshots + log access for the web UI
# ---------------------------------------------------------------------------

async def _sh(cmd: str, timeout: float = 10) -> str:
  try:
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
    out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return out_b.decode(errors="replace").strip()
  except Exception as e:
    return f"(error: {e})"


def _file_hash(path: str) -> str:
  try:
    with open(path, "rb") as f:
      return hashlib.sha1(f.read()).hexdigest()[:12]
  except Exception:
    return "(missing)"


def _append_jsonl(name: str, record: dict, max_bytes: int = _PULSE_MAX_BYTES) -> None:
  try:
    os.makedirs(TRIAGE_DIR, exist_ok=True)
    path = os.path.join(TRIAGE_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) >= max_bytes:
      os.replace(path, path + ".1")
    record.setdefault("t", round(time.time(), 2))  # noqa: TID251 (wall clock is correct for log records)
    with open(path, "a") as f:
      f.write(json.dumps(record, separators=(",", ":")) + "\n")
  except Exception:
    pass


async def _code_identity() -> dict:
  """Everything needed to prove whether the code on disk changed (hypothesis A)."""
  ident = {
    "branch": await _sh(f"{_GIT} rev-parse --abbrev-ref HEAD"),
    "commit": await _sh(f"{_GIT} rev-parse --short HEAD"),
    "dirty": await _sh(f"{_GIT} status --porcelain | head -5"),
    "version": await _sh("cat /data/openpilot/FUNNYPILOT_VERSION"),
    "updater_target": await _sh("cat /data/params/d/UpdaterTargetBranch 2>/dev/null || echo '(unset)'"),
    "staged": await _sh("git -c safe.directory='*' -C /data/safe_staging/finalized rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(none)'"),
    "overlay_consistent": os.path.exists("/data/safe_staging/.overlay_consistent"),
    "hashes": {os.path.basename(p): _file_hash(p) for p in _FEEL_FILES},
  }
  try:
    with open("/proc/sys/kernel/random/boot_id") as f:
      ident["boot_id"] = f.read().strip()
    with open("/proc/uptime") as f:
      ident["uptime_s"] = round(float(f.read().split()[0]), 1)
  except Exception:
    pass
  return ident


async def _boot_snapshot() -> None:
  ident = await _code_identity()
  ident["kind"] = "boot"
  _append_jsonl("code_identity.jsonl", ident)


async def _pulse_task() -> None:
  """Every 10 min, log the code identity — if something swaps the code while the
  car sits parked, this pins down WHEN it happened, not just that it happened."""
  last = None
  while True:
    try:
      ident = await _code_identity()
      key = (ident.get("branch"), ident.get("commit"), json.dumps(ident.get("hashes", {}), sort_keys=True),
             ident.get("updater_target"), ident.get("staged"), bool(ident.get("dirty")))
      if key != last:
        # identity changed (or first pulse): always record, flag the change
        ident["kind"] = "pulse-change" if last is not None else "pulse-start"
        _append_jsonl("code_identity.jsonl", ident)
        last = key
      else:
        _append_jsonl("code_identity.jsonl", {"kind": "pulse-ok", "commit": ident.get("commit"),
                                              "uptime_s": ident.get("uptime_s")})
    except Exception:
      pass
    await asyncio.sleep(_PULSE_PERIOD_S)


async def handle_logs_list(request: web.Request) -> web.Response:
  try:
    files = []
    if os.path.isdir(TRIAGE_DIR):
      for name in sorted(os.listdir(TRIAGE_DIR)):
        if _TRIAGE_NAME_RE.match(name):
          p = os.path.join(TRIAGE_DIR, name)
          st = os.stat(p)
          files.append({"name": name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return web.json_response({"dir": TRIAGE_DIR, "files": files})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_logs_get(request: web.Request) -> web.Response:
  try:
    name = request.match_info["name"]
    if not _TRIAGE_NAME_RE.match(name):
      return web.json_response({"error": "bad name"}, status=400)
    path = os.path.join(TRIAGE_DIR, name)
    if not os.path.isfile(path):
      return web.json_response({"error": "not found"}, status=404)
    tail_kb = min(int(request.query.get("tail_kb", "128")), 2048)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
      if size > tail_kb * 1024:
        f.seek(size - tail_kb * 1024)
        f.readline()  # drop the partial first line
      text = f.read().decode(errors="replace")
    return web.json_response({"name": name, "size": size, "truncated": size > tail_kb * 1024, "text": text})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def handle_logs_mark(request: web.Request) -> web.Response:
  """User-aligned ground truth: 'the issue is happening RIGHT NOW'."""
  try:
    note = ""
    try:
      body = await request.json()
      note = str(body.get("note", ""))[:500]
    except Exception:
      pass
    _append_jsonl("marks.jsonl", {"kind": "user-mark", "note": note})
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"error": str(e)}, status=500)


async def _start_triage_background(app: web.Application) -> None:
  await _boot_snapshot()
  app["triage_pulse"] = asyncio.create_task(_pulse_task())


async def handle_index(request: web.Request) -> web.Response:
  index_path = os.path.join(_STATIC_DIR, "index.html")
  if os.path.exists(index_path):
    with open(index_path, "r") as f:
      content = f.read()
    return web.Response(content_type="text/html", text=content)
  return web.Response(text="FunnyPilot Terminal Server", content_type="text/html")


def main():
  app = web.Application()
  app.router.add_get("/", handle_index)
  app.router.add_get("/ws", handle_ws)
  app.router.add_get("/api/branches", handle_branches)
  app.router.add_post("/api/flash", handle_flash)
  app.router.add_post("/api/diagnostics", handle_diagnostics)
  app.router.add_get("/api/logs", handle_logs_list)
  app.router.add_get("/api/logs/{name}", handle_logs_get)
  app.router.add_post("/api/logs/mark", handle_logs_mark)
  app.on_startup.append(_start_triage_background)

  if os.path.isdir(_STATIC_DIR):
    app.router.add_static("/static", _STATIC_DIR)

  web.run_app(app, host="0.0.0.0", port=_PORT, reuse_address=True)


if __name__ == "__main__":
  main()
