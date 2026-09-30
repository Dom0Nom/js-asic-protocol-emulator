"""Recorded host sessions: the CLI's commands run against the chip model with
device models on the board, and every transport call is logged.

`tools/mpy_replay.py` replays a log under the MicroPython unix port: the
same host library code runs, each `xfer` must send exactly the recorded
bytes and gets the recorded reply, and `ticks_us`/`sleep_us` follow the
recording. The CLI output must match CPython's line for line. That checks
the host library is MicroPython-compatible on the code paths the use cases
actually take, without hardware.
"""

import binascii
import json

from .harness import Harness
from .transport import model_transport


class RecordingTransport:
    def __init__(self, inner):
        self.t = inner
        self.clock_hz = inner.clock_hz
        self.events = []

    def xfer(self, data):
        rx = self.t.xfer(data)
        self.events.append(["x", binascii.hexlify(bytes(data)).decode(), binascii.hexlify(rx).decode()])
        return rx

    def sleep_us(self, us):
        self.events.append(["s", int(us)])
        self.t.sleep_us(us)

    def ticks_us(self):
        v = self.t.ticks_us()
        self.events.append(["t", v])
        return v


def _i2c(h):
    from .models.i2c import Eeprom24Target, I2cTarget
    h.board.pullups |= {"uio4", "uio5"}
    h.attach(I2cTarget("uio5", "uio4", 0x68, regs=bytearray(range(256)), mode="fast"))
    h.attach(Eeprom24Target("uio5", "uio4", size=256, page=8, abytes=1, mode="fast"))


def _flash(h):
    from .models.spi import SpiNorFlash
    mem = bytearray((i * 7 + 3) & 0xFF for i in range(1 << 16))
    h.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=0, mem=mem))


def _jtag(h):
    from .models.jtag import JtagChain, Tap
    h.board.pullups |= {"uio2"}
    h.attach(JtagChain("uio0", "uio1", "uio2", "uio3", [Tap(4, 0x4BA00477), Tap(5, None), Tap(6, 0x0362D093)]))


def _swd(h):
    from .models.swd import SwdTarget
    h.board.pullups |= {"uio0"}
    h.attach(SwdTarget("uio1", "uio0", wait_count=2, pwrup_delay=100))


def _uart(h):
    from .models.uart import UartTx
    h.board.pullups |= {"uio1"}
    tx = h.attach(UartTx("uio1", 115200, baud_error=0.01))
    tx.send(b"U\x00Boot 1.0\r\n" * 3, gap_bits=3)


def _square(h):
    from .models.siggen import Square
    h.attach(Square("uio0", high=300, low=700, start=5000))


def _pulses(h):
    from .models.siggen import Pulses
    # The loader now runs a cleanup image first. Start after both images
    # are loaded; pulses during loading are not armed trigger events.
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


def record(name):
    """Run one scenario on the model. Returns (events, output lines, clock)."""
    from tempo_host.cli import Cli
    from tempo_host.link import Link
    setup, lines = SCENARIOS[name]
    h = Harness(None, boot="host")
    setup(h)
    rt = RecordingTransport(model_transport(h))
    out = []
    cli = Cli(Link(rt), out=out.append)
    for ln in lines:
        out.append("> " + ln)
        cli.run(ln)
    return rt.events, out, rt.clock_hz


def save(name, path):
    events, out, clock_hz = record(name)
    with open(path, "w") as f:
        json.dump({"name": name, "clock_hz": clock_hz, "commands": SCENARIOS[name][1],
                   "events": events, "output": out}, f)
    return out
