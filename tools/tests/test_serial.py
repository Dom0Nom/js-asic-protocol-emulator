"""PC CLI and serial line protocol against the chip model: a fake serial port
feeds each request line to the board-side handler (host/tempo_host/serve.py)
running on a model-backed Link, so both ends of the protocol are the real
code; only the USB cable is simulated."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tempo_cli  # noqa: E402
from tempo import sessions  # noqa: E402
from tempo.harness import Harness  # noqa: E402
from tempo.transport import model_transport  # noqa: E402
from tempo_host.cli import Cli  # noqa: E402
from tempo_host.link import Link  # noqa: E402
from tempo_host.serve import handle  # noqa: E402


class FakeSerial:
    def __init__(self, link):
        self.link = link
        self.cli = Cli(link, self._out)
        self.lines = []

    def _out(self, s):
        self.lines.append((s + "\r\n").encode())

    def write(self, b):
        self.cli.out = self._out
        handle(self.link, self.cli, b.decode(), self._out)

    def readline(self):
        return self.lines.pop(0)


def board(scenario):
    h = Harness(None, boot="host")
    sessions.SCENARIOS[scenario][0](h)
    return FakeSerial(Link(model_transport(h)))


def test_command_runs_on_board():
    ser = board("flash")
    out = []
    assert tempo_cli.board_command(ser, "flash id", out.append)
    assert out[0].startswith("JEDEC ID ef 40 18"), out
    assert not tempo_cli.board_command(ser, "flash nonsense", out.append)


def test_pc_side_library_over_forwarded_transactions(tmp_path):
    ser = board("flash")
    out = []
    raw = []
    cli = Cli(Link(tempo_cli.SerialTransport(ser)), lambda s: (raw if s.startswith("raw ") else out).append(s))
    assert cli.run("flash read 0x10 32 raw=1")
    data = bytes.fromhex(raw[0][4:])
    assert data == bytes((i * 7 + 3) & 0xFF for i in range(0x10, 0x30))


def test_model_mode_writes_flash_dump(tmp_path, capsys):
    f = tmp_path / "dump.bin"
    assert tempo_cli.main(["--model", "flash", "flash read 0 64", "--out", str(f)]) == 0
    assert f.read_bytes() == bytes((i * 7 + 3) & 0xFF for i in range(64))
    assert "wrote 64 bytes" in capsys.readouterr().out
