"""Biased blocking-instruction programs and their coverage tracker (model only)."""

import random

from tempo import randprog
from tempo.blockcov import BlockCov
from tempo.board import Device
from tempo.harness import Harness
from tempo.models.hostspi import HostDriver


class Writer(HostDriver):
    def __init__(self, seed):
        super().__init__()
        self.rng = random.Random(seed)

    def step(self, cycle, nets):
        if not self.busy and self.rng.random() < 0.05:
            if self.rng.random() < 0.6:
                self.mbox_write([self.rng.getrandbits(16) for _ in range(self.rng.randint(1, 5))])
            else:
                self.mbox_read(self.rng.randint(1, 4))
        return super().step(cycle, nets)


class Noise(Device):
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.levels = {f"ui{i}": 0 for i in range(5)}

    def step(self, cycle, nets):
        for n in self.levels:
            if self.rng.random() < 0.02:
                self.levels[n] ^= 1
        return dict(self.levels)


def test_blocking_program_blocks_and_wakes():
    total = BlockCov(None)
    for seed in range(1, 6):
        h = Harness(randprog.blocking_program(seed))
        h.attach(Noise(seed))
        h.attach(Writer(seed))
        cov = BlockCov(h.chip)
        for _ in range(1500):
            cov.before()
            h.step()
            cov.after()
        assert not h.chip.illegal
        total.merge(cov)
    for n in ("wev", "hget", "rx"):
        assert total.blocked[n] > 0 and total.woke[n] > 0, total.report()
    assert total.multi["wev"] + total.multi["hget"] + total.multi["rx"] > 0


def test_tracker_counts_pending_event_as_immediate():
    h = Harness(".entry 0, a\n.entry 1, b\na: sev 2\n sev 2\n wev 2\n halt\nb: wev 3\n halt\n")
    cov = BlockCov(h.chip)
    for _ in range(60):
        cov.before()
        h.step()
        cov.after()
    assert cov.immediate["wev"] == 1 and cov.woke["wev"] == 0
    assert cov.blocked["wev"] > 0  # thread 1 never gets event 3
