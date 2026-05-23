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
    env["PATH"] = ":".join(path_entries) if path_entries else "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
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

  if os.path.isdir(_STATIC_DIR):
    app.router.add_static("/static", _STATIC_DIR)

  web.run_app(app, host="0.0.0.0", port=_PORT, reuse_address=True)


if __name__ == "__main__":
  main()
