"""
FunnyPilot terminal server — port 8888.

  GET /          → terminal web UI (index.html)
  GET /ws        → WebSocket pty-backed bash shell
  POST /api/flash → flash a branch from funnypilot remote and reboot
"""

from __future__ import annotations

import asyncio
import os
import pty
import shlex
import struct
import fcntl
import termios

from aiohttp import web, WSMsgType

PORT = 8888
WEB_DIR = os.path.join(os.path.dirname(__file__), "nav_web")
OPENPILOT_DIR = "/data/openpilot"
FUNNYPILOT_REMOTE = "https://github.com/jgray-dev/funnypilot.git"


async def index(request):
  return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def ws_terminal(request):
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  master_fd, slave_fd = pty.openpty()
  pid = os.fork()
  if pid == 0:
    os.close(master_fd)
    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
    os.dup2(slave_fd, 0)
    os.dup2(slave_fd, 1)
    os.dup2(slave_fd, 2)
    if slave_fd > 2:
      os.close(slave_fd)
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["HOME"] = "/root"
    env["SHELL"] = "/bin/bash"
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    os.execve("/bin/bash", ["/bin/bash", "-l"], env)
    os._exit(1)

  os.close(slave_fd)
  loop = asyncio.get_event_loop()

  async def read_pty():
    try:
      while True:
        data = await loop.run_in_executor(None, lambda: os.read(master_fd, 4096))
        if not data:
          break
        await ws.send_bytes(data)
    except OSError:
      pass
    finally:
      await ws.close()

  read_task = asyncio.ensure_future(read_pty())

  async for msg in ws:
    if msg.type == WSMsgType.BINARY:
      data = msg.data
      if len(data) >= 1 and data[0] == 0xFF:
        # resize: FF <rows_hi> <rows_lo> <cols_hi> <cols_lo>
        if len(data) == 5:
          rows = (data[1] << 8) | data[2]
          cols = (data[3] << 8) | data[4]
          winsize = struct.pack("HHHH", rows, cols, 0, 0)
          fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
      else:
        try:
          os.write(master_fd, data)
        except OSError:
          break
    elif msg.type == WSMsgType.TEXT:
      try:
        os.write(master_fd, msg.data.encode())
      except OSError:
        break
    elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
      break

  read_task.cancel()
  try:
    os.close(master_fd)
  except OSError:
    pass
  try:
    os.waitpid(pid, os.WNOHANG)
  except ChildProcessError:
    pass

  return ws


async def api_flash(request):
  try:
    body = await request.json()
    branch = body.get("branch", "").strip()
  except Exception:
    return web.json_response({"error": "Invalid JSON"}, status=400)

  if not branch:
    return web.json_response({"error": "Missing branch"}, status=400)

  # Sanitize: only allow alphanumeric, dashes, dots, underscores
  import re
  if not re.match(r'^[\w.\-]+$', branch):
    return web.json_response({"error": "Invalid branch name"}, status=400)

  script = (
    # Stop updated daemon first so it doesn't detect the git change and show a UI prompt
    f"sudo systemctl stop updated 2>/dev/null; "
    # Ensure funnypilot remote points to HTTPS (handles both add and existing SSH URL)
    f"git -C {shlex.quote(OPENPILOT_DIR)} remote set-url funnypilot {FUNNYPILOT_REMOTE} 2>/dev/null "
    f"|| git -C {shlex.quote(OPENPILOT_DIR)} remote add funnypilot {FUNNYPILOT_REMOTE}; "
    f"git -C {shlex.quote(OPENPILOT_DIR)} fetch funnypilot && "
    f"git -C {shlex.quote(OPENPILOT_DIR)} checkout {shlex.quote(branch)} && "
    f"git -C {shlex.quote(OPENPILOT_DIR)} reset --hard funnypilot/{shlex.quote(branch)} && "
    f"sudo reboot"
  )

  proc = await asyncio.create_subprocess_shell(
    script,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT,
  )
  asyncio.ensure_future(_wait_proc(proc))
  return web.json_response({"ok": True, "branch": branch})


async def _wait_proc(proc):
  await proc.wait()


async def api_branches(request):
  """Proxy GitHub branch list so browser CORS issues are avoided."""
  import urllib.request
  import json
  url = "https://api.github.com/repos/jgray-dev/funnypilot/branches?per_page=100"
  try:
    req = urllib.request.Request(url, headers={"User-Agent": "FunnyPilot/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
      data = json.loads(resp.read())
    branches = sorted(
      [b["name"] for b in data if b["name"].startswith("funnypilot-")],
      reverse=True,
    )
    return web.json_response(branches)
  except Exception as e:
    return web.json_response({"error": str(e)}, status=503)


def main():
  app = web.Application()
  app.router.add_get("/", index)
  app.router.add_get("/index.html", index)
  app.router.add_static("/static", WEB_DIR)
  app.router.add_get("/ws", ws_terminal)
  app.router.add_post("/api/flash", api_flash)
  app.router.add_get("/api/branches", api_branches)

  web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, reuse_address=True)


if __name__ == "__main__":
  main()
