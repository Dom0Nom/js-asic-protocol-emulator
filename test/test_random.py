# SPDX-License-Identifier: Apache-2.0
"""Constrained-random lockstep: random multi-threaded programs on the RTL
and the model, compared on every pad and every commit, every cycle."""

import os
import random

import cocotb

from lockstep import Lockstep
from tempo import randprog
from tempo.board import Device

SEEDS = int(os.environ.get("RAND_SEEDS", "8"))
CYCLES = int(os.environ.get("RAND_CYCLES", "3000"))
BASE = int(os.environ.get("RAND_BASE", "1"))


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
    for seed in range(BASE, BASE + SEEDS):
        dut._log.info(f"seed {seed}")
        ls = Lockstep(dut, randprog.program(seed), boot="backdoor")
        ls.attach(Noise(seed))
        await ls.start()
        await ls.run(CYCLES)
        total_cycles += ls.cycles
        total_commits += ls.commits
    dut._log.info(f"random lockstep: {SEEDS} seeds, {total_cycles} cycles, {total_commits} commits matched")
