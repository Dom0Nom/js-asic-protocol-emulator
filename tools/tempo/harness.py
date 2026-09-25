"""Run the chip model against board devices."""

import os

from . import asm
from .board import Board
from .sim import Chip

FW = os.path.join(os.path.dirname(__file__), "..", "..", "fw")


# Output index 15 (host MISO) is on uo[3], the RP2350's SPI0 RX pin, so
# output index 11 takes uo[7]. Inputs are not permuted.
OUT_PAD = {11: 7, 15: 3}


def net_of_output(i):
    return f"uio{i}" if i < 8 else f"uo{OUT_PAD.get(i, i - 8)}"


def pads_to_index(uo):
    """Physical uo_out byte -> output index bits [15:8]."""
    v = 0
    for i in range(8, 16):
        v |= ((uo >> OUT_PAD.get(i, i - 8)) & 1) << (i - 8)
    return v


def net_of_input(i):
    return f"uio{i}" if i < 8 else f"ui{i - 8}"


class Harness:
    def __init__(self, src, clock_hz=50e6, pullups=(), threads=None, chip=None, path=None,
                 boot="backdoor"):
        """boot: "backdoor" loads IMEM and starts threads directly; "host"
        loads and starts them through the host SPI port, like real silicon."""
        self.prog = asm.assemble(src or "", FW if path is None else path)
        self.chip = chip or Chip()
        self.board = Board(clock_hz=clock_hz, pullups=set(pullups))
        self.drive_ui = {}  # host-driven dedicated inputs {index: level}
        self.host = None
        if boot == "host":
            from .models.hostspi import HostDriver
            self.host = self.attach(HostDriver())
            self.host.boot(self.prog, threads)
        else:
            self.chip.load(self.prog.words)
            for t, pc in sorted(self.prog.entries.items()):
                if threads is None or t in threads:
                    self.chip.start(t, pc)
        self.trace = []  # (cycle, value, oe) when the pads change

    def attach(self, dev):
        return self.board.attach(dev)

    def step(self):
        nets = self.board.nets
        pads_in = 0
        for i in range(16):
            n = net_of_input(i)
            if i >= 8 and (i - 8) in self.drive_ui:
                lvl = self.drive_ui[i - 8]
            else:
                lvl = nets.get(n, 1 if n in self.board.pullups else 0)
            pads_in |= (lvl & 1) << i
        val, oe = self.chip.step(pads_in)
        drives = {}
        for i in range(16):
            if i < 8:
                drives[net_of_output(i)] = (val >> i) & 1 if (oe >> i) & 1 else None
            else:
                drives[net_of_output(i)] = (val >> i) & 1
        if not self.trace or self.trace[-1][1:] != (val, oe):
            self.trace.append((self.board.cycle, val, oe))
        self.board.tick(drives)

    def run(self, cycles):
        for _ in range(int(cycles)):
            self.step()

    def boot_done(self, limit=200000):
        """Run until the host driver has finished its queued transactions."""
        return self.run_until(lambda: not self.host.busy, limit)

    def run_until(self, cond, limit):
        for _ in range(int(limit)):
            if cond():
                return True
            self.step()
        return cond()
