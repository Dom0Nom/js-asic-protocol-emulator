"""Host SPI port model (docs/architecture.md, Host port).

Works on the synchronised pins like the RTL: HSCK is input index 13
(ui[5]), HMOSI 14 (ui[6]), HCS_N 15 (ui[7]); HMISO is output index 15, which
the top level puts on uo[3]. These are the RP2350's SPI0 SCK, TX and RX pins
on the demo board. MOSI is sampled on a synchronised
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
        self.rcnt = 0  # words a mailbox read may still pop, or a write may still push
        self.peeked = False  # mailbox read: the head word is being shifted out
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
        sck = (pins >> 13) & 1
        mosi = (pins >> 14) & 1
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
                # past the header bytes only the index parity matters, so a
                # long transaction wraps 255 -> 128 instead of saturating
                self.idx = self.idx + 1 if self.idx < 255 else 128
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

    def error(self) -> int:
        """1 if any error flag is set: illegal instruction, thread LATE,
        PINCONFLICT, or a lane LATE/UNDER/STUFFERR/OVERRUN/ARBLOST."""
        ch = self.chip
        e = ch.pinconflict or any(t.illegal or t.late for t in ch.threads)
        for lane in ch.lanes:
            e = e or bool(lane.status() & 0x3E0)
        return int(bool(e))

    def byte(self, i, b) -> int:
        """Handle transaction byte i; return the byte to shift out next."""
        ch = self.chip
        if i == 0:
            self.cmd = b
            if b == 0x09:
                # the byte after the command carries the T2H level; the read
                # pops at most that many words
                self.rcnt = len(ch.t2h)
                self.peeked = False
                return self.rcnt | self.error() << 7
            if b == 0x08:
                # ... and for a write, the free H2T space; the write pushes at
                # most that many words and drops the rest
                self.rcnt = 4 - len(ch.h2t)
                return self.rcnt | self.error() << 7
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
            if 5 <= i <= 8:
                lane = ch.lanes[i - 5]
                st = lane.status()
                return (st >> 3) & 0x7F | (st & 1) << 7
            return 0
        if c == 0x08:
            if i % 2 == 1:
                self.hi = b
            elif self.rcnt:
                self.rcnt -= 1
                self.ops.append(("h2t", (self.hi << 8) | b))
            return 0
        if c == 0x09:
            # Peek at the head when the byte before its high byte completes
            # (MISO must be loaded then); pop only once the high byte has
            # been clocked out, so a host that stops after n words loses
            # nothing.
            if i % 2 == 1:
                self.peeked = self.rcnt > 0
                self.word = ch.t2h[0] if self.peeked else 0
                return self.word >> 8
            if self.peeked:
                self.peeked = False
                self.rcnt -= 1
                self.ops.append(("t2h_pop",))
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
