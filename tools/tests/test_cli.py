"""The host library's command line (host/tempo_host/cli.py) against the chip
model with device models on the pins.

    python tools/tests/test_cli.py i2c     # print one scenario's output
"""

import os
import sys

import pytest

sys.path[:0] = [os.path.join(os.path.dirname(__file__), ".."), os.path.join(os.path.dirname(__file__), "..", "..", "host")]

from tempo.harness import Harness  # noqa: E402
from tempo.transport import model_transport  # noqa: E402
from tempo_host.cli import Cli  # noqa: E402
from tempo_host.link import Link  # noqa: E402


def _i2c(h):
    from tempo.models.i2c import Eeprom24Target, I2cTarget
    h.board.pullups |= {"uio4", "uio5"}
    h.attach(I2cTarget("uio5", "uio4", 0x68, regs=bytearray(range(256)), mode="fast"))
    h.attach(Eeprom24Target("uio5", "uio4", size=256, page=8, abytes=1, mode="fast"))


def _flash(h):
    from tempo.models.spi import SpiNorFlash
    mem = bytearray((i * 7 + 3) & 0xFF for i in range(1 << 16))
    h.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=0, mem=mem))


def _jtag(h):
    from tempo.models.jtag import JtagChain, Tap
    h.board.pullups |= {"uio2"}
    h.attach(JtagChain("uio0", "uio1", "uio2", "uio3", [Tap(4, 0x4BA00477), Tap(5, None), Tap(6, 0x0362D093)]))


def _swd(h):
    from tempo.models.swd import SwdTarget
    h.board.pullups |= {"uio0"}
    h.attach(SwdTarget("uio1", "uio0", wait_count=2, pwrup_delay=100))


def _uart(h):
    from tempo.models.uart import UartTx
    h.board.pullups |= {"uio1"}
    tx = h.attach(UartTx("uio1", 115200, baud_error=0.01))
    tx.send(b"U\x00Boot 1.0\r\n" * 3, gap_bits=3)


def _square(h):
    from tempo.models.siggen import Square
    h.attach(Square("uio0", high=300, low=700, start=5000))


def _pulses(h):
    from tempo.models.siggen import Pulses
    # start after the image is loaded: earlier pulses are not armed trigger events
    h.attach(Pulses("uio1", [100000 + 1000 * k for k in range(9)]))


# name: (board setup, command lines)
SCENARIOS = {
    "i2c": (_i2c, ["i2c scan", "i2c dump 0x68 n=16 reg=0x10", "eeprom read 0 16", "status"]),
    "flash": (_flash, ["flash id", "flash sfdp", "flash read 0x100 48"]),
    "jtag": (_jtag, ["jtag idcode"]),
    "swd": (_swd, ["swd attach"]),
    "uart": (_uart, ["uart autobaud rx=1"]),
    "la": (_square, ["la capture 0 us=3000 n=40"]),
    "trigger": (_pulses, ["trigger edge 1 0 3 300 20 n=3 us=20000"]),
}

EXPECT = {
    "i2c": ["found: 0x50 0x68", "000010  10 11 12 13", "000000  ff ff ff ff"],
    "flash": ["JEDEC ID ef 40 18", "SFDP rev 1.", "000100  "],
    "jtag": ["3 device(s)", "Xilinx", "no IDCODE (BYPASS)", "Arm"],
    "swd": ["DPIDR 0x2ba01477: Arm", "ROM table at 0xe00ff000", "component"],
    "uart": ["nearest standard rate 115200"],
    "la": ["duty 0.300, high 300..300 low 700..700 cycles"],
    "trigger": ["3 pulse(s) generated"],
}


def run(name):
    setup, lines = SCENARIOS[name]
    h = Harness(None, boot="host")
    setup(h)
    out = []
    cli = Cli(Link(model_transport(h)), out=out.append)
    for ln in lines:
        out.append("> " + ln)
        cli.run(ln)
    return out


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_cli(name):
    text = "\n".join(run(name))
    assert "error:" not in text, text
    for frag in EXPECT[name]:
        assert frag in text, (frag, text)


if __name__ == "__main__":
    print("\n".join(run(sys.argv[1])))
