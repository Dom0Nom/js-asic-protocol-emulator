# SPDX-License-Identifier: Apache-2.0
"""Blocking instructions (wev, hget, rx, hput) in RTL-vs-model lockstep.

Directed cases sweep the wake phase against every thread slot, the event
already pending, several threads blocked on one source, FIFO full and
overrun. A biased-random mode (`randprog.blocking_program`) adds seeds.
Coverage comes from `tempo.blockcov` on the model side of the lockstep,
which the RTL matches pad for pad and commit for commit.

    make -B COCOTB_TEST_MODULES=test_blocking                 # directed + 20 biased seeds
    BLOCK_SEEDS=0 make -B COCOTB_TEST_MODULES=test_blocking   # directed only
"""

import os
import random

import cocotb

from lockstep import Lockstep
from tempo import isa, randprog
from tempo.blockcov import BlockCov
from tempo.board import Device
from tempo.models.hostspi import HostDriver

SEEDS = int(os.environ.get("BLOCK_SEEDS", "20"))
CYCLES = int(os.environ.get("BLOCK_CYCLES", "1500"))
BASE = int(os.environ.get("BLOCK_BASE", "1"))

TOTAL = BlockCov(None)


class Tracked(Lockstep):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cov = BlockCov(self.chip)

    async def cycle(self):
        self.cov.before()
        await super().cycle()
        self.cov.after()


class Noise(Device):
    def __init__(self, seed, rate=0.05):
        self.rng = random.Random(seed)
        self.rate = rate
        self.levels = {f"ui{i}": 0 for i in range(5)}

    def step(self, cycle, nets):
        for n in self.levels:
            if self.rng.random() < self.rate:
                self.levels[n] ^= 1
        return dict(self.levels)


class BiasedHost(HostDriver):
    """Write-heavy random host traffic, so hget has words to take."""

    def __init__(self, seed, rate=0.05):
        super().__init__()
        self.rng = random.Random(seed ^ 0x5A5A)
        self.rate = rate

    def step(self, cycle, nets):
        if not self.busy and self.rng.random() < self.rate:
            k = self.rng.random()
            if k < 0.6:
                self.mbox_write([self.rng.getrandbits(16) for _ in range(self.rng.randint(1, 5))])
            elif k < 0.9:
                self.mbox_read(self.rng.randint(1, 4))
            else:
                self.status()
        return super().step(cycle, nets)


def log_cov(dut, title, cov):
    dut._log.info(title)
    for line in cov.report():
        dut._log.info("  " + line)


async def run_case(dut, src, noise=None, host=False):
    ls = Tracked(dut, src, boot="backdoor")
    if noise is not None:
        ls.attach(Noise(noise))
    if host:
        ls.h.host = ls.attach(HostDriver())
    await ls.start()
    return ls


def done(dut, name, ls):
    TOTAL.merge(ls.cov)
    log_cov(dut, f"{name}: {ls.cycles} cycles, {ls.commits} commits matched", ls.cov)
    assert not ls.chip.illegal, ls.chip.illegal


# Producer T0 raises events 0, 1, 2 for T1, T2, T3 after a delay that walks
# 0..15 cycles, then waits for each consumer's ack (events 3, 4, 5). The
# consumer sees the event 1, 2 or 3 cycles after sev, so every slot phase of
# a wake is covered, and T0's own acks arrive both before and after it waits.
WEV_PHASE = """
.entry 0, p
.entry 1, c1
.entry 2, c2
.entry 3, c3
p:      li r2, 0
p_loop: now r6
        add r6, r6, r2
        waitt r6
        sev 0
        sev 1
        sev 2
        wev 3
        wev 4
        wev 5
        addi r2, 1
        andi r2, 15
        j p_loop
c1:     wev 0
        addi r1, 1
        sev 3
        j c1
c2:     wev 1
        nop
        addi r1, 1
        sev 4
        j c2
c3:     wev 2
        now r6
        addi r6, 9
        waitt r6
        sev 5
        j c3
"""

# Three threads blocked on event 6 at once; each sev wakes exactly one.
# T0 also raises events 7 (twice: the second sev finds it already set) and
# 5 on itself before waiting (already pending), and sets event 6 on two and
# three consecutive slots so the blocked threads wake back to back.
WEV_MULTI = """
.entry 0, p
.entry 1, c
.entry 2, c
.entry 3, c
p:      sev 7
        sev 7
        wev 7
        sev 5
        wev 5
        now r6
        addi r6, 40
        waitt r6
        sev 6
        now r6
        addi r6, 30
        waitt r6
        sev 6
        sev 6
        now r6
        addi r6, 30
        waitt r6
        sev 6
        sev 6
        sev 6
        addi r3, 1
        j p
c:      wev 6
        addi r1, 1
        tid r2
        cmpi r2, 2
        bne c
        nop
        j c
"""

# All four threads start blocked on hget; each echoes what it takes with
# hput after a per-thread delay (15, 7, 30, 90 cycles), so words pile up in
# H2T (pending, full) and T2H fills and blocks hput.
HGET = """
.entry 0, t0
.entry 1, t1
.entry 2, t2
.entry 3, t3
t0:     hget r1
        now r6
        addi r6, 15
        waitt r6
        hput r1
        j t0
t1:     hget r1
        now r6
        addi r6, 7
        waitt r6
        hput r1
        j t1
t2:     hget r1
        now r6
        addi r6, 30
        waitt r6
        hput r1
        j t2
t3:     hget r1
        now r6
        addi r6, 90
        waitt r6
        hput r1
        j t3
"""


# Lane 0 receives from ui0 (noise), lane 1 from ui1. T0 queues receives on
# lane 0 in bursts; T1 and T2 both block in rx on lane 0, and after each
# word the flags rx set pick a branch, so a flag mismatch shows up as a
# commit PC mismatch. T3 queues one receive on lane 1 and takes it at once
# (blocks), then queues three while not reading (RX FIFO depth 2: the third
# overruns), takes the two pending words back to back and reads the
# overrun bit with stat (a register write the lockstep compares).
def rx_src():
    pins0 = 15 | 8 << 4 | 15 << 8 | 15 << 12
    pins1 = 15 | 9 << 4 | 15 << 8 | 15 << 12
    return f"""
.entry 0, p
.entry 1, c
.entry 2, c
.entry 3, s
p:      lc r7, {pins0}
        lw l0, pins, r7
        li r7, 1
        lw l0, len0, r7
        lw l0, len1, r7
        li r2, 0
p_loop: rxn l0, 4
        now r6
        addi r6, 25
        waitt r6
        rxn l0, 8
        rxn l0, 3
        rxn l0, 5
        now r6
        addi r6, 120
        add r6, r6, r2
        waitt r6
        addi r2, 3
        andi r2, 31
        j p_loop
c:      rx r1, l0
        bne c_nz
        addi r3, 1
c_nz:   bcc c_ce
        addi r4, 1
c_ce:   bpl c
        addi r5, 1
        j c
s:      lc r7, {pins1}
        lw l1, pins, r7
        li r7, 2
        lw l1, len0, r7
        lw l1, len1, r7
s_loop: rxn l1, 6
        rx r1, l1
        rxn l1, 2
        rxn l1, 2
        rxn l1, 2
        now r6
        addi r6, 60
        waitt r6
        rx r2, l1
        rx r2, l1
        stat r3, l1
        j s_loop
"""


@cocotb.test()
async def test_wev_phase(dut):
    ls = await run_case(dut, WEV_PHASE)
    await ls.run(1400)
    done(dut, "wev_phase", ls)
    c = ls.cov
    assert c.woke["wev"] >= 60 and c.immediate["wev"] >= 10, c.report()
    assert {1, 2, 3} <= set(c.latency["wev"]) or {0, 1, 2} <= set(c.latency["wev"]), c.latency["wev"]


@cocotb.test()
async def test_wev_multi(dut):
    ls = await run_case(dut, WEV_MULTI)
    await ls.run(1200)
    done(dut, "wev_multi", ls)
    c = ls.cov
    assert c.multi["wev"] >= 3 and c.immediate["wev"] >= 10, c.report()
    # every sev 6 wakes exactly one of the three waiters
    woke = sum(ls.chip.threads[t].regs[1] for t in (1, 2, 3))
    sevs = sum(1 for r in ls.chip.retires if r.tid == 0 and isa.decode(r.word).name == "sev"
               and isa.decode(r.word).rs == 6)
    assert sevs - 1 <= woke <= sevs, (woke, sevs)


@cocotb.test()
async def test_hget(dut):
    ls = await run_case(dut, HGET, host=True)
    rng = random.Random(7)
    sent, got = [], []
    await ls.run(40)
    for burst in [1, 2, 3, 4, 5, 6, 1, 1, 8, 2, 12, 7, 4, 10]:
        words = [rng.getrandbits(16) for _ in range(burst)]
        sent += words
        for _ in range(200):
            if not words:
                break
            t = ls.h.host.mbox_write(words[:4])
            await ls.boot()
            words = words[min(t.rx[1] & 7, len(words[:4])):]
            if words:
                # H2T full: the threads may all be blocked in hput on a full
                # T2H, so drain T2H before retrying (the model's count is the
                # oracle the RTL is compared against every cycle)
                n = len(ls.chip.t2h)
                if n:
                    got += await ls.mbox_read(n)
                await ls.run(20)
        assert not words, "host could not write the burst"
        await ls.run(rng.choice([0, 3, 20, 60]))
    await ls.run(200)
    while len(got) < len(sent):
        got += await ls.mbox_read(min(4, len(sent) - len(got)))
    done(dut, "hget", ls)
    assert sorted(got) == sorted(sent), (sent, got)
    c = ls.cov
    assert c.woke["hget"] >= 5 and c.immediate["hget"] >= 5 and c.multi["hget"] >= 1, c.report()


@cocotb.test()
async def test_rx(dut):
    ls = await run_case(dut, rx_src(), noise=11)
    await ls.run(2000)
    done(dut, "rx", ls)
    c = ls.cov
    assert c.woke["rx"] >= 10 and c.immediate["rx"] >= 5 and c.multi["rx"] >= 3, c.report()
    stats = [r.value for r in ls.chip.retires if r.tid == 3 and isa.decode(r.word).name == "stat"]
    assert stats and all(v >> 8 & 1 for v in stats), stats  # STAT.OVERRUN after every third rxn


@cocotb.test()
async def test_blocking_random(dut):
    total = BlockCov(None)
    for seed in range(BASE, BASE + SEEDS):
        dut._log.info(f"seed {seed}")
        ls = Tracked(dut, randprog.blocking_program(seed), boot="backdoor")
        ls.attach(Noise(seed, 0.02))
        ls.attach(BiasedHost(seed))
        await ls.start()
        await ls.run(CYCLES)
        total.merge(ls.cov)
        assert not ls.chip.illegal, (seed, ls.chip.illegal)
        dut._log.info(f"seed {seed}: {ls.commits} commits matched; retired "
                      + " ".join(f"{n}={ls.cov.woke[n] + ls.cov.immediate[n]}" for n in ("wev", "hget", "rx", "hput")))
    TOTAL.merge(total)
    log_cov(dut, f"biased random: {SEEDS} seeds x {CYCLES} cycles (base {BASE})", total)
    log_cov(dut, "all blocking cases in this run", TOTAL)
