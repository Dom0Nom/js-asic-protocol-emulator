"""Run the RTL and the cycle-level model side by side on one board.

Every cycle the same pad inputs go to both; the pads the RTL drives must
equal the model's on every cycle, and every instruction commit (thread, PC,
register write) must match. Board devices see the RTL's outputs, so the
protocol models check the RTL while the model checks it cycle by cycle.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly  # noqa: E402

from tempo import isa  # noqa: E402
from tempo.harness import Harness, net_of_input, net_of_output  # noqa: E402


class Mismatch(AssertionError):
    pass


class Lockstep:
    def __init__(self, dut, src, pullups=(), threads=None, gl=False, boot="host"):
        """boot="host": load and start through the host SPI port (both sides).
        boot="backdoor": preload the SRAM and PCs and set the run bits in
        the first cycle on both sides (fast, for random tests)."""
        self.dut = dut
        self.boot_mode = boot
        self.threads = threads
        self.h = Harness(src, pullups=pullups, threads=threads, boot=boot)
        if boot == "backdoor":
            for t in self.h.chip.threads:
                t.run = False  # started together with the RTL in start()
        self.chip = self.h.chip
        self.board = self.h.board
        self.gl = gl or os.environ.get("GATES") == "yes"
        self.core = None if self.gl else dut.user_project.core
        self.cycles = 0
        self.commits = 0
        self.pending = None  # model retire expected on the RTL commit port next cycle

    def attach(self, dev):
        return self.board.attach(dev)

    async def start(self):
        d = self.dut
        cocotb.start_soon(Clock(d.clk, 20, unit="ns").start())
        d.ena.value = 1
        d.ui_in.value = 0
        d.uio_in.value = 0
        d.rst_n.value = 0
        await ClockCycles(d.clk, 5)
        await FallingEdge(d.clk)
        d.rst_n.value = 1  # this falling edge starts model cycle 0
        if not self.gl:
            # Real SRAM powers up random; the model assumes 0. Firmware must
            # only read words it loaded, and the bench zero-fills both sides.
            mem = d.user_project.imem.i_SRAM_1P_behavioral_bm_bist.memory
            for a in range(1024):
                mem[a].value = self.h.prog.words[a] if self.boot_mode == "backdoor" else 0
        if self.boot_mode == "backdoor":
            core = d.user_project.core
            run = 0  # noqa
            for t, pc in sorted(self.h.prog.entries.items()):
                if self.threads is None or t in self.threads:
                    core.pc[t].value = pc
                    self.chip.threads[t].pc = pc
                    self.chip.threads[t].run = True
                    run |= 1 << t
            core.run.value = run

    def pads_in(self):
        nets = self.board.nets
        v = 0
        for i in range(16):
            n = net_of_input(i)
            v |= (nets.get(n, 1 if n in self.board.pullups else 0) & 1) << i
        return v

    async def cycle(self):
        d = self.dut
        pads = self.pads_in()
        d.uio_in.value = pads & 0xFF
        d.ui_in.value = pads >> 8
        await ReadOnly()
        uo = int(d.uo_out.value)
        uio = int(d.uio_out.value)
        oe = int(d.uio_oe.value)
        rtl_val = uo << 8 | uio
        rtl_oe = 0xFF00 | oe
        n_ret = len(self.chip.retires)
        m_val, m_oe = self.chip.step(pads)
        m_val_eff = (m_val & (m_oe | 0xFF00)) & 0xFFFF
        rtl_val_eff = rtl_val & rtl_oe
        if rtl_oe != (m_oe | 0xFF00) or rtl_val_eff != m_val_eff:
            raise Mismatch(f"cycle {self.cycles}: pads rtl val {rtl_val_eff:04x} oe {rtl_oe:04x}, "
                           f"model val {m_val_eff:04x} oe {m_oe | 0xFF00:04x}; last commits {self.chip.retires[-4:]}")
        if self.core is not None:
            self.check_commit()
        self.pending = self.chip.retires[n_ret] if len(self.chip.retires) > n_ret else None
        drives = {}
        for i in range(16):
            if i < 8:
                drives[net_of_output(i)] = (rtl_val >> i) & 1 if (rtl_oe >> i) & 1 else None
            else:
                drives[net_of_output(i)] = (rtl_val >> i) & 1
        self.board.tick(drives)
        self.cycles += 1
        await FallingEdge(d.clk)

    def check_commit(self):
        c = self.core
        rtl = int(c.commit_o.value)
        exp = self.pending
        if bool(rtl) != (exp is not None):
            raise Mismatch(f"cycle {self.cycles}: commit rtl={rtl} model={exp}; {self.rtl_state()}")
        if exp is None:
            return
        self.commits += 1
        got = (int(c.commit_tid.value), int(c.commit_pc.value))
        if got != (exp.tid, exp.pc):
            raise Mismatch(f"cycle {self.cycles}: commit rtl T{got[0]} pc {got[1]:03x}, model {exp}")
        if exp.rd is not None:
            if not int(c.commit_wr.value) or int(c.commit_rd.value) != exp.rd or int(c.commit_val.value) != exp.value:
                raise Mismatch(f"cycle {self.cycles}: write rtl r{int(c.commit_rd.value)}="
                               f"{int(c.commit_val.value):#06x} wr={int(c.commit_wr.value)}, model {exp}")
        elif int(c.commit_wr.value):
            raise Mismatch(f"cycle {self.cycles}: rtl wrote r{int(c.commit_rd.value)}, model {exp}")

    def rtl_state(self):
        c = self.core
        try:
            pcs = [int(c.pc[i].value) for i in range(4)]
        except Exception:
            pcs = "?"
        return (f"rtl run={int(c.run.value):04b} irv={int(c.irv.value)} ir={int(c.ir.value):04x} "
                f"pc={pcs} now={int(c.now.value)}; model run={[t.run for t in self.chip.threads]} "
                f"pc={[t.pc for t in self.chip.threads]} now={self.chip.now}")

    async def run(self, n):
        for _ in range(int(n)):
            await self.cycle()

    async def run_until(self, cond, limit):
        for _ in range(int(limit)):
            if cond():
                return True
            await self.cycle()
        return cond()

    async def boot(self, limit=400000):
        ok = await self.run_until(lambda: not self.h.host.busy, limit)
        assert ok, "host transactions did not finish"
