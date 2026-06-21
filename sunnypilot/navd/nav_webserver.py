"""
FunnyPilot terminal server — PTY-backed bash shell over WebSocket at /ws.
Branch selector sorted by last commit date (most recent first).
POST /api/flash  — hard-sets device to chosen funnypilot branch and reboots.
GET  /api/branches — list funnypilot branches, newest first.
"""
import asyncio
import json
import os
import pty
import fcntl
import termios
import struct
import aiohttp
from aiohttp import web

REPO = "jgray-dev/funnypilot"
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "nav_web")
_SHELL = "/bin/bash"
_PORT = 8888

# Expected version for the running branch (used by /api/diagnostics).
EXPECTED_VERSION = "3.2.1st"

# Read-only checks that verify the on-device code matches what we shipped and
# capture state for diagnosing the "interp feels deactivated" issue. All commands
# are non-mutating. `-c safe.directory=*` avoids git "dubious ownership" failures
# when the webserver uid differs from the checkout owner.
_GIT = "git -c safe.directory='*' -C /data/openpilot"
DIAG_CHECKS = [
  {"id": "version",        "name": "FunnyPilot version",                 "cmd": "cat /data/openpilot/FUNNYPILOT_VERSION 2>&1"},
  {"id": "branch",         "name": "Git branch",                         "cmd": f"{_GIT} rev-parse --abbrev-ref HEAD 2>&1"},
  {"id": "head",           "name": "Git HEAD commit",                    "cmd": f"{_GIT} log -1 --format='%h %s' 2>&1"},
  {"id": "clean",          "name": "Working tree unmodified",            "cmd": f"{_GIT} status --porcelain 2>&1"},
  {"id": "diff",           "name": "Tracked-file changes (diff stat)",   "cmd": f"{_GIT} diff --stat 2>&1"},
  {"id": "code_controlsd", "name": "controlsd 3.2.1e code present",       "cmd": "grep -c 'v3.2.1e' /data/openpilot/selfdrive/controls/controlsd.py 2>&1"},
  {"id": "code_latctrl",   "name": "latcontrol re-engage ramp present",  "cmd": "grep -c '_REENGAGE_RAMP_DUR' /data/openpilot/selfdrive/controls/lib/latcontrol_torque.py 2>&1"},
  {"id": "code_blinker",   "name": "blinker settle-time present",        "cmd": "grep -c 'UNWIND_SETTLE_TIME' /data/openpilot/sunnypilot/selfdrive/controls/lib/blinker_pause_lateral.py 2>&1"},
  {"id": "interp_shm",     "name": "INTERP heartbeat (/dev/shm)",        "cmd": "cat /dev/shm/lat_interp 2>/dev/null || echo '(absent — not driving)'"},
  {"id": "model_bundle",   "name": "Active model bundle",                "cmd": "cat /data/params/d/ModelManager_ActiveBundle 2>/dev/null || echo '(none / stock)'"},
  {"id": "staging",        "name": "Updater staging dir",               "cmd": "ls -la /data/safe_staging/ 2>&1 || echo '(none)'"},
  {"id": "overlay",        "name": "Overlay mounts (updater)",           "cmd": "mount 2>/dev/null | grep -i overlay || echo '(none)'"},
]


def _eval_diag(check_id: str, out: str):
  """Return (level, summary) for a check. level in pass|fail|warn|info."""
  s = out.strip()
  if check_id == "version":
    return ("pass", s) if s == EXPECTED_VERSION else ("warn", f"{s or '(empty)'} (expected {EXPECTED_VERSION})")
  if check_id == "branch":
    return ("pass" if "3.2.1st" in s else "warn", s or "(unknown)")
  if check_id == "clean":
    return ("pass", "clean") if s == "" else ("fail", "MODIFIED")
  if check_id == "diff":
    return ("pass", "no changes") if s == "" else ("fail", "files differ")
  if check_id.startswith("code_"):
    try:
      n = int(s.splitlines()[0])
    except Exception:
      n = 0
    return ("pass", "present") if n >= 1 else ("fail", "MISSING")
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

    script = (
      f"cd /data/openpilot && "
      f"sudo git -c http.sslVerify=false fetch funnypilot {branch} && "
      f"sudo git checkout {branch} && "
      f"sudo git reset --hard funnypilot/{branch} && "
      f"sudo reboot"
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

  if os.path.isdir(_STATIC_DIR):
    app.router.add_static("/static", _STATIC_DIR)

  web.run_app(app, host="0.0.0.0", port=_PORT, reuse_address=True)


if __name__ == "__main__":
  main()
