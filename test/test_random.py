# SPDX-License-Identifier: Apache-2.0
"""Constrained-random lockstep: random multi-threaded programs on the RTL
and the model, compared on every pad and every commit, every cycle."""

import collections
import os
import random

import cocotb

from lockstep import Lockstep
from tempo import isa, randprog
from tempo.board import Device
from tempo.models.hostspi import HostDriver

SEEDS = int(os.environ.get("RAND_SEEDS", "8"))
CYCLES = int(os.environ.get("RAND_CYCLES", "3000"))
BASE = int(os.environ.get("RAND_BASE", "1"))


class RandomHost(HostDriver):
    """Random host-port traffic while the threads run: mailbox writes and
    reads, status reads. Exercises the host port against the model."""

    def __init__(self, seed):
        super().__init__()
        self.rng = random.Random(seed ^ 0x5A5A)

    def step(self, cycle, nets):
        if not self.busy and self.rng.random() < 0.01:
            k = self.rng.randint(0, 2)
            if k == 0:
                self.mbox_write([self.rng.getrandbits(16) for _ in range(self.rng.randint(1, 5))])
            elif k == 1:
                self.mbox_read(self.rng.randint(1, 4))
            else:
                self.status()
        return super().step(cycle, nets)


class Noise(Device):
    """Random levels on the dedicated inputs ui[0..4]."""

    def __init__(self, seed, rate=0.02):
        self.rng = random.Random(seed)
        self.rate = rate
        self.levels = {f"ui{i}": 0 for i in range(5)}

    def step(self, cycle, nets):
        for n in self.levels:
            if self.rng.random() < self.rate:
                self.levels[n] ^= 1
        return dict(self.levels)


@cocotb.test()
async def test_random_programs(dut):
    total_cycles = total_commits = 0
    cov = collections.Counter()
    xclk_rx = 0
    for seed in range(BASE, BASE + SEEDS):
        dut._log.info(f"seed {seed}")
        ls = Lockstep(dut, randprog.program(seed), boot="backdoor")
        ls.attach(Noise(seed))
        ls.attach(RandomHost(seed))
        await ls.start()
        await ls.run(CYCLES)
        total_cycles += ls.cycles
        total_commits += ls.commits
        cov.update(isa.decode(r.word).name for r in ls.chip.retires)
        xclk_rx += sum(getattr(lane, "xclk_samples", 0) for lane in ls.chip.lanes)
    dut._log.info(f"random lockstep: {SEEDS} seeds, {total_cycles} cycles, {total_commits} commits matched")
    names = sorted(set(n for n in isa.ALU3_FN + isa.ALU2_FN + list(isa.ALU2_SHIFT.values()) + isa.IMM9_OPS
                       + ["lih", "ori", "xori", "ld", "st", "ldi", "b", "djnz", "j", "call", "lw", "lr",
                          "tx", "txcrc"] + isa.SYS_FN + isa.LANE_SUB))
    dut._log.info("instruction coverage (retired): " + " ".join(f"{n}={cov[n]}" for n in names))
    dut._log.info(f"external-clock receive samples: {xclk_rx}")
    missing = [n for n in names if cov[n] == 0]
    if missing:
        dut._log.warning(f"never retired: {missing}")
