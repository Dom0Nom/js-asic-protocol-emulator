"""Run the chip model against board devices."""

import os

from . import asm
from .board import Board
from .sim import Chip

FW = os.path.join(os.path.dirname(__file__), "..", "..", "fw")


def net_of_output(i):
    return f"uio{i}" if i < 8 else f"uo{i - 8}"


def net_of_input(i):
    return f"uio{i}" if i < 8 else f"ui{i - 8}"


class Harness:
    def __init__(self, src, clock_hz=50e6, pullups=(), threads=None, chip=None, path=None):
        self.prog = asm.assemble(src, FW if path is None else path)
        self.chip = chip or Chip()
        self.chip.load(self.prog.words)
        self.board = Board(clock_hz=clock_hz, pullups=set(pullups))
        self.drive_ui = {}  # host-driven dedicated inputs {index: level}
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
            lvl = self.drive_ui.get(i - 8) if i >= 8 and (i - 8) in self.drive_ui else nets.get(n, 1 if n in self.board.pullups else 0)
            pads_in |= (lvl & 1) << i
        val, oe = self.chip.step(pads_in)
        drives = {}
        for i in range(15):
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

    def run_until(self, cond, limit):
        for _ in range(int(limit)):
            if cond():
                return True
            self.step()
        return cond()
