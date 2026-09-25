"""Host SPI port model (docs/architecture.md, Host port).

Works on the synchronised pins like the RTL: HMOSI is input index 13
(ui[5]), HSCK 14 (ui[6]), HCS_N 15 (ui[7]). MOSI is sampled on a synchronised
SCK rising edge, and MISO moves to the next bit on that same edge (a few
cycles after the host sampled it). The byte shifted out during
transaction byte k+1 is loaded when byte k completes.
"""

from . import isa

MASK16 = 0xFFFF
IMEM_MASK = isa.IMEM_WORDS - 1


class HostPort:
    def __init__(self, chip):
        self.chip = chip
        self.cs_active = False
        self.sck_prev = 0
        self.bitcnt = 0
        self.sh_in = 0
        self.mo = 0
        self.idx = 0
        self.cmd = 0
        self.hi = 0
        self.addr = 0
        self.thread = 0
        self.reg = 0
        self.word = 0
        self.wr_pend = None  # (addr, data)
        self.rd_pend = None  # addr
        self.rdata = 0
        self.ops = []  # applied after the thread executes (pre-edge reads)

    @property
    def miso(self):
        return (self.mo >> 7) & 1

    # IMEM access in cycles whose fetch slot belongs to a halted thread.
    def imem_slot(self, fetch_thread):
        ch = self.chip
        if ch.threads[fetch_thread].run:
            return
        if self.wr_pend is not None:
            a, d = self.wr_pend
            ch.imem[a] = d
            self.wr_pend = None
        elif self.rd_pend is not None:
            self.rdata = ch.imem[self.rd_pend]
            self.rd_pend = None

    def clock(self):
        pins = self.chip.pins_in
        cs = not (pins >> 15) & 1
        sck = (pins >> 14) & 1
        mosi = (pins >> 13) & 1
        if not cs:
            self.cs_active = False
            self.sck_prev = sck
            return
        if not self.cs_active:
            self.cs_active = True
            self.bitcnt = self.idx = 0
            self.mo = 0
        rise = sck and not self.sck_prev
        self.sck_prev = sck
        if rise:
            # The host has just sampled MISO; present the next bit now, which
            # gives it a whole SCK period of slack instead of half.
            self.sh_in = ((self.sh_in << 1) | mosi) & 0xFF
            self.bitcnt = (self.bitcnt + 1) & 7
            if self.bitcnt == 0:
                self.mo = self.byte(self.idx, self.sh_in) & 0xFF
                self.idx = min(self.idx + 1, 255)
            else:
                self.mo = (self.mo << 1) & 0xFF

    def apply(self):
        ch = self.chip
        for op in self.ops:
            if op[0] == "control":
                b = op[1]
                for t in range(isa.NTHREADS):
                    th = ch.threads[t]
                    if (b >> t) & 1:
                        th.run = True
                        th.illegal = False
                    if (b >> (t + 4)) & 1:
                        th.run = False
                        th.step = False
            elif op[0] == "pc":
                ch.threads[op[1]].pc = op[2]
            elif op[0] == "h2t":
                ch.h2t.append(op[1])
            elif op[0] == "t2h_pop":
                ch.t2h.popleft()
            elif op[0] == "step":
                th = ch.threads[op[1]]
                th.run = True
                th.step = True
        self.ops = []

    def byte(self, i, b) -> int:
        """Handle transaction byte i; return the byte to shift out next."""
        ch = self.chip
        if i == 0:
            self.cmd = b
            return 0
        c = self.cmd
        if c in (0x01, 0x02):  # IMEM write / read
            if i == 1:
                self.hi = b
                return 0
            if i == 2:
                self.addr = ((self.hi << 8) | b) & IMEM_MASK
                if c == 0x02:
                    self.rd_pend = self.addr
                return 0
            if c == 0x01:
                if (i - 3) % 2 == 0:
                    self.hi = b
                else:
                    self.wr_pend = (self.addr, (self.hi << 8) | b)
                    self.addr = (self.addr + 1) & IMEM_MASK
                return 0
            if (i - 3) % 2 == 0:
                return self.rdata >> 8
            out = self.rdata & 0xFF
            self.addr = (self.addr + 1) & IMEM_MASK
            self.rd_pend = self.addr
            return out
        if c == 0x05:
            if i == 1:
                self.ops.append(("control", b))
            return 0
        if c == 0x06:
            if i == 1:
                self.thread = b & 3
            elif i == 2:
                self.hi = b
            elif i == 3:
                if not ch.threads[self.thread].run:
                    self.ops.append(("pc", self.thread, ((self.hi << 8) | b) & IMEM_MASK))
            return 0
        if c == 0x07:
            if i == 1:
                return sum(ch.threads[t].run << t | ch.threads[t].illegal << (t + 4) for t in range(4))
            if i == 2:
                return min(len(ch.t2h), 15) << 4 | min(len(ch.h2t), 15)
            if i == 3:
                f = 0
                for lane in ch.lanes:
                    f |= (lane.status() >> 3) & 0x7F
                return f | ch.pinconflict << 7
            if i == 4:
                return sum(ch.threads[t].late << t for t in range(4))
            return 0
        if c == 0x08:
            if i % 2 == 1:
                self.hi = b
            elif len(ch.h2t) < 4:
                self.ops.append(("h2t", (self.hi << 8) | b))
            return 0
        if c == 0x09:
            if i % 2 == 1:
                self.word = ch.t2h[0] if ch.t2h else 0
                if ch.t2h:
                    self.ops.append(("t2h_pop",))
                return self.word >> 8
            return self.word & 0xFF
        if c == 0x0A:
            if i == 1:
                if not ch.threads[b & 3].run:
                    self.ops.append(("step", b & 3))
            return 0
        if c == 0x0B:
            if i == 1:
                self.thread = b & 3
                return 0
            if i == 2:
                self.reg = b & 15
                return 0
            if i == 3:
                th = ch.threads[self.thread]
                if th.run:
                    self.word = 0
                elif self.reg < 8:
                    self.word = th.regs[self.reg]
                elif self.reg == 8:
                    self.word = th.pc
                else:
                    self.word = th.flags()
                return self.word >> 8
            if i == 4:
                return self.word & 0xFF
            return 0
        return 0
