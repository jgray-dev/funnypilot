"""Real HDLC framing with a fake serial fd; no modem or device mutations."""
from types import SimpleNamespace as NS
import pytest
from openpilot.system.qcomgpsd import modemdiag as M


def diag(monkeypatch, chunks):
  monkeypatch.setattr(M.ModemDiag, 'open_serial', lambda self: NS(fd=17, read=lambda n: chunks.pop(0)))
  return M.ModemDiag()


def test_silent_modem_times_out_for_manager_restart(monkeypatch):
  d = diag(monkeypatch, [])
  waits = []
  monkeypatch.setattr(M.select, 'select', lambda r,w,x,t: waits.append(t) or ([],[],[]))
  with pytest.raises(TimeoutError):
    d.recv()
  assert len(waits) == 1 and 0 < waits[0] <= 10.


def test_frame_fragments_and_pending_second_frame(monkeypatch):
  d = diag(monkeypatch, [])
  packet = d.hdlc_encapsulate(bytes([16])+b'hello')
  chunks = [packet[:2], packet[2:]+packet]
  d.serial.read = lambda n: chunks.pop(0)
  monkeypatch.setattr(M.select, 'select', lambda *a: ([17],[],[]))
  assert d.recv() == (16, b'hello')
  assert d.recv() == (16, b'hello')
  assert not chunks


@pytest.mark.parametrize('chunk,exception', [(b'', OSError), (b'x'*65536, ValueError)])
def test_disconnect_and_unterminated_garbage_are_bounded(monkeypatch, chunk, exception):
  d = diag(monkeypatch, [chunk]*5)
  monkeypatch.setattr(M.select, 'select', lambda *a: ([17],[],[]))
  with pytest.raises(exception):
    d.recv()


def test_unrelated_log_flood_cannot_hide_missing_command_reply(monkeypatch):
  clock = iter(range(20))
  monkeypatch.setattr(M.time, 'monotonic', lambda: next(clock))
  d = NS(send=lambda *a: None, recv=lambda **kw: (M.DIAG_LOG_F, b''))
  with pytest.raises(TimeoutError):
    M.send_recv(d, M.DIAG_LOG_CONFIG_F, b'')
