"""
FunnyPilot terminal server — aiohttp + WebSocket pseudo-terminal on port 8888.

Serves a browser-based terminal that spawns a bash shell on the comma device,
providing SSH-like access from any browser on the local network.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios

from aiohttp import web, WSMsgType

PORT = 8888
WEB_DIR = os.path.join(os.path.dirname(__file__), "nav_web")

_COLS = 220
_ROWS = 50


def _set_winsize(fd: int, rows: int, cols: int) -> None:
  try:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
  except Exception:
    pass


async def index(request):
  return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def ws_terminal(request):
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  pid, master_fd = pty.fork()
  if pid == 0:
    # child: exec bash with a clean environment
    env = {
      "TERM": "xterm-256color",
      "HOME": os.path.expanduser("~"),
      "USER": os.environ.get("USER", "comma"),
      "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
      "PYTHONPATH": "/data/openpilot",
    }
    os.execvpe("/bin/bash", ["/bin/bash", "--login"], env)
    os._exit(1)

  # parent: wire pty ↔ websocket
  _set_winsize(master_fd, _ROWS, _COLS)
  fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
  fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

  loop = asyncio.get_event_loop()
  pty_closed = asyncio.Event()

  def _pty_readable():
    try:
      data = os.read(master_fd, 4096)
      if data:
        asyncio.ensure_future(ws.send_bytes(data))
    except OSError:
      pty_closed.set()
      loop.remove_reader(master_fd)

  loop.add_reader(master_fd, _pty_readable)

  try:
    async for msg in ws:
      if pty_closed.is_set():
        break
      if msg.type == WSMsgType.BINARY:
        try:
          os.write(master_fd, msg.data)
        except OSError:
          break
      elif msg.type == WSMsgType.TEXT:
        # resize: {"type":"resize","cols":N,"rows":N}
        import json
        try:
          ev = json.loads(msg.data)
          if ev.get("type") == "resize":
            _set_winsize(master_fd, int(ev["rows"]), int(ev["cols"]))
        except Exception:
          pass
      elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
        break
  finally:
    loop.remove_reader(master_fd)
    try:
      os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
      pass
    try:
      os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
      pass
    try:
      os.close(master_fd)
    except OSError:
      pass

  return ws


def main():
  app = web.Application()
  app.router.add_get("/", index)
  app.router.add_get("/index.html", index)
  app.router.add_static("/static", WEB_DIR)
  app.router.add_get("/ws", ws_terminal)

  web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, reuse_address=True)


if __name__ == "__main__":
  main()
