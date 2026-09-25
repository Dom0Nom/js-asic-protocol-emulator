"""Cycle-level reference model of the Tempo chip.

`Chip.step(pads_in)` models one clock cycle: it returns the pad outputs
driven during the cycle (flop outputs), then applies the rising clock edge
using `pads_in` as the pad input levels sampled at that edge.

Thread t executes on cycles where NOW % 4 == t. An instruction reads state
as it is during that cycle and all of its effects become visible from the
next cycle, exactly like a register update at the clock edge. The RTL
aligns its execute stage with these cycles, so pad traces match cycle for
cycle.
"""

from collections import deque
from dataclasses import dataclass, field

from . import isa
from .host import HostPort
from .lane import Lane

MASK16 = 0xFFFF
FIFO_DEPTH = 4


def after(a: int, b: int) -> bool:
    """True when time a is at or after time b (mod 2^16, half range)."""
    return ((a - b) & MASK16) < 0x8000


@dataclass
class Thread:
    tid: int
    pc: int = 0
    regs: list = field(default_factory=lambda: [0] * isa.NREGS)
    z: int = 0
    c: int = 0
    n: int = 0
    run: bool = False
    step: bool = False  # host single step: halt again after one commit
    illegal: bool = False
    fetch_ok: bool = False  # fetched in its fetch slot while running
    ir: int = 0  # word read from IMEM in the fetch slot
    ldi_addr: int = 0
    ldi_rd: int = 0
    ldi_word: int = 0  # trace only
    retired: int = 0
    stalls: int = 0
    late: int = 0
    ldi_phase: int = 0  # ldi takes two slots

    def flags(self) -> int:
        return self.z | self.c << 1 | self.n << 2


class Retire:
    """Record of one executed (non-stalled) instruction, for lockstep."""
    __slots__ = ("cycle", "tid", "pc", "word", "rd", "value")

    def __init__(self, cycle, tid, pc, word, rd=None, value=None):
        self.cycle, self.tid, self.pc, self.word = cycle, tid, pc, word
        self.rd, self.value = rd, value

    def __repr__(self):
        w = f" r{self.rd}={self.value:#06x}" if self.rd is not None else ""
        return f"@{self.cycle} T{self.tid} {self.pc:03x}: {isa.disasm(self.word)}{w}"


class Chip:
    def __init__(self, sync_stages: int = 2):
        self.now = 0
        self.imem = [0] * isa.IMEM_WORDS
        self.scratch = [0] * isa.SCRATCH_WORDS
        self.threads = [Thread(t) for t in range(isa.NTHREADS)]
        self.lanes = [Lane(i, self) for i in range(isa.NLANES)]
        self.host = HostPort(self)
        self.gout = 0
        self.goe = 0
        self.ev = 0
        self.h2t = deque()
        self.t2h = deque()
        self.sync = [0] * sync_stages  # input synchroniser, [-1] is visible
        self.prev_in = 0
        self.out_reg = (0, 0)  # registered pad value/enable
        self.retires = []  # list of Retire, appended as instructions execute
        self.trace_retires = True
        self.illegal = []
        self.pinconflict = False

    # ---- host-side helpers (transaction level; the SPI port model wraps these)
    def load(self, words, base=0):
        for i, w in enumerate(words):
            self.imem[base + i] = w & MASK16

    def start(self, tid, pc=None):
        """Backdoor start: the run bit is set from the next cycle on."""
        t = self.threads[tid]
        if pc is not None:
            t.pc = pc
        t.run = True

    # ---- pins
    @property
    def pins_in(self) -> int:
        """Synchronised input pins as seen by logic this cycle."""
        return self.sync[-1]

    def pad_outputs(self):
        """(value, enable) for the 16 output indices during this cycle."""
        val, oe = self.gout & 0x7FFF | self.host.miso << 15, self.goe | 0xFF00
        seen = 0
        for lane in reversed(self.lanes):  # lane 0 has highest priority
            lv, lo, owned = lane.pin_drive()
            if seen & owned:
                self.pinconflict = True
            seen |= owned
            val = (val & ~owned) | (lv & owned)
            oe = (oe & ~owned) | (lo & owned)
        return val & MASK16, oe & MASK16

    # ---- one clock cycle
    def step(self, pads_in: int = 0):
        out = self.out_reg
        out_next = self.pad_outputs()  # output flops sample the current state
        # Everything below computes next state from current state.
        # Fetch slot: IMEM is read for the thread that executes two cycles later,
        # or given to the host if that thread is halted.
        ft = self.threads[(self.now + 2) % isa.NTHREADS]
        ft.fetch_ok = ft.run
        if ft.run:
            ft.ir = self.imem[(ft.ldi_addr if ft.ldi_phase else ft.pc) % isa.IMEM_WORDS]
        self.host.imem_slot(ft.tid)
        self.host.clock()
        tid = self.now % isa.NTHREADS
        t = self.threads[tid]
        if t.run and t.fetch_ok:
            t.fetch_ok = False
            self.execute(t)
        for lane in self.lanes:
            lane.clock()
            lane.commit()
        self.host.apply()
        self.out_reg = out_next
        self.prev_in = self.sync[-1]
        self.sync = [pads_in & MASK16] + self.sync[:-1]
        self.now = (self.now + 1) & MASK16
        return out

    # ---- instruction execution
    def set_nz(self, t, v):
        t.z = int(v & MASK16 == 0)
        t.n = (v >> 15) & 1

    def execute(self, t: Thread):
        pc0 = t.pc
        w = t.ir
        i = isa.decode(w)
        r = t.regs
        nxt = (t.pc + 1) % isa.IMEM_WORDS
        wr = None  # (reg, value)
        name = i.name

        def stall():
            t.stalls += 1

        if t.ldi_phase:
            # second slot of ldi: the fetched word is the data
            t.ldi_phase = 0
            r[t.ldi_rd] = t.ir
            t.pc = (t.pc + 1) % isa.IMEM_WORDS
            return self.commit(t, pc0, t.ldi_word, (t.ldi_rd, t.ir))
        if name == "ill":
            self.illegal.append((self.now, t.tid, t.pc, w))
            t.run = False
            t.illegal = True
            t.step = False
            return
        if i.op == isa.OP_ALU3:
            a, b = r[i.rs], r[i.rt]
            sh = b & 15
            if name == "add":
                v = a + b
                t.c = v >> 16
            elif name == "sub":
                v = a - b
                t.c = int(a >= b)
            elif name == "and":
                v = a & b
            elif name == "or":
                v = a | b
            elif name == "xor":
                v = a ^ b
            elif name == "shl":
                v = a << sh
                if sh:
                    t.c = (a >> (16 - sh)) & 1
            elif name == "shr":
                v = a >> sh
                if sh:
                    t.c = (a >> (sh - 1)) & 1
            else:  # asr
                v = isa.sext(a, 16) >> sh
                if sh:
                    t.c = (a >> (sh - 1)) & 1
            v &= MASK16
            self.set_nz(t, v)
            wr = (i.rd, v)
        elif i.op == isa.OP_ALU2:
            a, d = r[i.rs], r[i.rd]
            k = i.imm
            if name == "mov":
                wr = (i.rd, a)
            elif name == "not":
                wr = (i.rd, ~a & MASK16)
            elif name == "neg":
                wr = (i.rd, -a & MASK16)
            elif name == "cmp":
                self.set_nz(t, d - a)
                t.c = int(d >= a)
            elif name == "tst":
                self.set_nz(t, d & a)
            elif name == "par":
                wr = (i.rd, bin(a).count("1") & 1)
            elif name == "brev":
                wr = (i.rd, int(f"{a:016b}"[::-1], 2))
            elif name == "swab":
                wr = (i.rd, ((a << 8) | (a >> 8)) & MASK16)
            elif name == "shli":
                wr = (i.rd, (a << k) & MASK16)
                if k:
                    t.c = (a >> (16 - k)) & 1
            elif name == "shri":
                wr = (i.rd, a >> k)
                if k:
                    t.c = (a >> (k - 1)) & 1
            elif name == "rori":
                wr = (i.rd, ((a >> k) | (a << (16 - k))) & MASK16)
            if wr is not None and name not in ("mov",):
                self.set_nz(t, wr[1])
        elif name == "li":
            wr = (i.rd, i.imm & MASK16)
        elif name == "addi":
            v = r[i.rd] + (i.imm & MASK16)
            t.c = v >> 16
            v &= MASK16
            self.set_nz(t, v)
            wr = (i.rd, v)
        elif name == "cmpi":
            b = i.imm & MASK16
            self.set_nz(t, r[i.rd] - b)
            t.c = int(r[i.rd] >= b)
        elif name == "lih":
            wr = (i.rd, (i.imm << 8) | (r[i.rd] & 0xFF))
        elif name in ("andi", "ori", "xori"):
            b = i.imm & MASK16
            v = {"andi": r[i.rd] & b, "ori": r[i.rd] | b, "xori": r[i.rd] ^ b}[name]
            self.set_nz(t, v)
            wr = (i.rd, v)
        elif name == "ld":
            wr = (i.rd, self.scratch[(r[i.rs] + i.imm) % isa.SCRATCH_WORDS])
        elif name == "st":
            self.scratch[(r[i.rs] + i.imm) % isa.SCRATCH_WORDS] = r[i.rd]
        elif name == "ldi":
            # first slot: the thread's next fetch reads the data word instead
            t.ldi_phase = 1
            t.ldi_addr = (r[i.rs] + i.imm) % isa.IMEM_WORDS
            t.ldi_rd = i.rd
            t.ldi_word = w
            return None
        elif name == "b":
            if self.cond(t, i.rt):
                nxt = (t.pc + 1 + i.imm) % isa.IMEM_WORDS
        elif name == "djnz":
            v = (r[i.rd] - 1) & MASK16
            wr = (i.rd, v)
            if v:
                nxt = (t.pc + 1 + i.imm) % isa.IMEM_WORDS
        elif name in ("j", "call"):
            if name == "call":
                wr = (7, nxt)
            nxt = i.imm % isa.IMEM_WORDS
        elif i.op == isa.OP_SYS:
            ra = r[i.rd]
            if name == "halt":
                t.run = False
            elif name == "jr":
                nxt = ra % isa.IMEM_WORDS
            elif name == "now":
                wr = (i.rd, self.now)
            elif name == "waitt":
                if not after(self.now, ra):
                    return stall()
                if ((self.now - ra) & MASK16) >= 4:
                    t.late = 1
            elif name == "ior":
                wr = (i.rd, self.pins_in)
            elif name == "iow":
                m = r[i.rs]
                self.gout = (self.gout & ~m) | (ra & m)
            elif name == "ioe":
                m = r[i.rs]
                self.goe = (self.goe & ~m) | (ra & m)
            elif name == "hput":
                if len(self.t2h) >= FIFO_DEPTH:
                    return stall()
                self.t2h.append(ra)
            elif name == "hget":
                if not self.h2t:
                    return stall()
                wr = (i.rd, self.h2t.popleft())
            elif name == "sev":
                self.ev |= 1 << i.rs
            elif name == "wev":
                if not (self.ev >> i.rs) & 1:
                    return stall()
                self.ev &= ~(1 << i.rs)
            elif name == "tid":
                wr = (i.rd, t.tid)
            elif name == "hcnt":
                wr = (i.rd, (FIFO_DEPTH - len(self.t2h)) << 8 | len(self.h2t))
            elif name == "wany":
                m = r[i.rs]
                ready = 0
                for k, lane in enumerate(self.lanes):
                    ready |= bool(lane.rxfifo) << k
                    ready |= lane.ev_start << (4 + k)
                ready |= bool(self.h2t) << 8
                ready |= (len(self.t2h) < FIFO_DEPTH) << 9
                ready |= (self.ev & 0x3F) << 10
                ready &= m
                if not ready and not after(self.now, ra):
                    return stall()
                wr = (i.rd, ready)
            elif name == "tstat":
                wr = (i.rd, t.late)
                t.late = 0
        else:
            res = self.lanes[i.lane].execute(i, r)
            if res is None:
                return stall()
            if res is not True:
                wr = (res[0], res[1] & MASK16)
        if wr is not None:
            r[wr[0]] = wr[1] & MASK16
        t.pc = nxt
        return self.commit(t, pc0, w, wr)

    def commit(self, t, pc0, w, wr):
        t.retired += 1
        if t.step:
            t.run = False
            t.step = False
        if self.trace_retires:
            self.retires.append(Retire(self.now, t.tid, pc0, w, *(wr if wr else (None, None))))
        return None

    @staticmethod
    def cond(t: Thread, c: int) -> bool:
        return [True, t.z, not t.z, t.c, not t.c, t.n, not t.n, t.c and not t.z][c]
