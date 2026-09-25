"""Lane model: a timed line-code engine (see docs/architecture.md, Lanes).

Clocking discipline, mirrored by the RTL:
  * `execute()` is called for a thread instruction during a cycle. It reads
    lane state as it is in that cycle and records deferred operations.
  * `clock()` computes the lane's own next state from the same pre-edge state.
  * `commit()` merges the deferred thread operations into the next state.
Merge rules: queue pushes land behind anything the lane popped; an RX FIFO
pop happens before the lane's push (so a full FIFO being popped does not
overrun); an event the lane sets in the same cycle survives a consume.
"""

from collections import deque

from . import isa

MASK16 = 0xFFFF
NONE_PIN = 15
QUEUE_DEPTH = 2
RX_DEPTH = 2
SYNC_LATENCY = 2  # two-flop input synchroniser

# Register indices
(CFG, PINS, LEN0, LEN1, SYM0, SYM1, FRAC, CR, CF, SAMP, PAT, PATE,
 CRC, POLY, CAP, MODE) = range(16)
REG_BITS = [14, 16, 16, 16, 16, 16, 8, 13, 13, 13, 16, 16, 16, 16, 16, 12]

# CFG bits
C_MSB, C_DOD, C_COD, C_CEN, C_CPOL, C_STRETCH, C_NRZI, C_DIFF = range(8)
C_STUFFANY, C_AUTOSTART, C_DREL = 11, 12, 13

# STAT bits
S_BUSY, S_QFULL, S_RXAV, S_START, S_STOP, S_LATE, S_UNDER, S_STUFFERR, S_OVERRUN, S_ARBLOST = range(10)
STICKY = sum(1 << b for b in (S_START, S_STOP, S_LATE, S_UNDER, S_STUFFERR, S_OVERRUN, S_ARBLOST))

# Features a lite lane does not have.
FULL_ONLY_CFG = (1 << C_NRZI) | (7 << 8) | (1 << C_STUFFANY)
FULL_ONLY_MODE = (1 << 4) | (3 << 5)  # CRCINV, RESYNC
FULL_ONLY_SYM = 1 << 13  # REL


def bit(v, n):
    return (v >> n) & 1


class Cmd:
    __slots__ = ("kind", "data", "n", "flags", "reg")

    def __init__(self, kind, data=0, n=0, flags=0, reg=0):
        self.kind, self.data, self.n, self.flags, self.reg = kind, data, n, flags, reg

    def __repr__(self):
        return f"Cmd({self.kind}, {self.data:#x}, n={self.n}, fl={self.flags})"


class Lane:
    def __init__(self, idx, chip, full=None):
        self.idx = idx
        self.chip = chip
        self.full = idx < 2 if full is None else full
        self.regs = [0] * 16
        self.regs[PINS] = 0xFFFF
        self.regs[LEN0] = self.regs[LEN1] = 2
        self.queue = deque()
        self.rxfifo = deque()  # entries: (word, count, end, err)
        self.stat = 0  # sticky bits only
        self.ev_start = 0
        self.ev_stop = 0
        self.last_count = 0
        self.pending_start = None
        # timeline
        self.cur = None  # running Cmd
        self.t = 0  # time units into the slot
        self.pre = 0  # cycles into the unit
        self.slot_len = 0
        self.acc = 0
        self.frozen = False
        self.slot_kind = None  # 'tx', 'rx', 'emit'
        self.slot_v = 0  # coded value of the current tx slot
        self.slot_sample = False
        self.bits_left = 0
        self.shift = 0
        self.tx_last = False
        self.arb_lost_cmd = False
        # coding state
        self.line = 1  # NRZI line state (coded level)
        self.run = 0
        self.last_bit = 0
        self.stuff_pending = False
        self.rline = 1
        self.rrun = 0
        self.rlast = 0
        self.destuff = False
        self.rword = 0
        self.rcount = 0
        self.rerr = 0
        self.prev_level = 0  # D level at the end of the previous slot (REL)
        # pins: level and enable for D, A, C, and which roles the lane owns
        self.d = [0, 0]
        self.a = [0, 0]
        self.c = [0, 0]
        self.owned = {"d": False, "a": False, "c": False}
        # inputs
        self.filt = [0, 0, 0, 0]  # filtered {D, I, C, A}
        self.fcnt = [0, 0, 0, 0]
        self.prev4 = 0
        self.frozen_done = False
        self.rx_complete = False
        self._m_start = 0
        self._prev_i = 0
        # deferred thread operations
        self.ops = []
        self.rx_new = []
        self.set_start = 0
        self.set_stop = 0

    # ------------------------------------------------------------ helpers
    def pin(self, role):
        p = self.regs[PINS]
        return {"d": p & 15, "i": (p >> 4) & 15, "c": (p >> 8) & 15, "a": (p >> 12) & 15}[role]

    def cfg(self, b):
        return bit(self.regs[CFG], b)

    @property
    def pre_bits(self):
        return self.regs[MODE] & 7

    def status(self):
        s = self.stat
        s |= (self.cur is not None or bool(self.queue)) << S_BUSY
        s |= (len(self.queue) >= QUEUE_DEPTH) << S_QFULL
        s |= bool(self.rxfifo) << S_RXAV
        s |= (self.ev_start << S_START) | (self.ev_stop << S_STOP)
        s |= min(self.last_count, 31) << 11
        return s

    def mask_reg(self, r, v):
        v &= (1 << REG_BITS[r]) - 1
        if not self.full:
            if r == CFG:
                v &= ~FULL_ONLY_CFG
            elif r == MODE:
                v &= ~FULL_ONLY_MODE
            elif r in (SYM0, SYM1):
                v &= ~FULL_ONLY_SYM
            elif r in (CRC, POLY):
                v = 0
        return v

    # ------------------------------------------------------------ outputs
    def pin_drive(self):
        """(value, enable, owned) masks over the 16 output indices."""
        val = oe = own = 0
        for role, (lvl, en) in (("d", self.d), ("a", self.a), ("c", self.c)):
            p = self.pin(role)
            if p == NONE_PIN or not self.owned[role]:
                continue
            own |= 1 << p
            val |= lvl << p
            oe |= en << p
        return val, oe, own

    # ------------------------------------------------------------ thread side
    def execute(self, i, regs):
        """Returns None to stall, True on success, or (reg, value) to write."""
        n = i.name
        qfull = len(self.queue) >= QUEUE_DEPTH
        if n == "lr":
            if i.imm == CAP:
                return (i.rd, self.regs[CAP])
            return (i.rd, self.regs[i.imm])
        if n in ("lw", "tx", "txcrc", "rxn", "emit", "start", "sync"):
            if qfull:
                return None
            if n == "lw":
                cmd = Cmd("lw", regs[i.rd], reg=i.imm)
            elif n in ("tx", "txcrc"):
                cmd = Cmd("tx", regs[i.rs], i.imm, i.rt)
            elif n == "rxn":
                cmd = Cmd("rx", 0, i.imm + 1, i.rd & 1)  # flag 1: continue the frame
            elif n == "emit":
                cmd = Cmd("emit", regs[i.rd])
            elif n == "start":
                cmd = Cmd("start", regs[i.rd])
            else:
                cmd = Cmd("start", (self.regs[CAP] + regs[i.rd]) & MASK16)
            self.ops.append(("push", cmd))
            return True
        if n == "rx":
            if not self.rxfifo:
                return None
            word, count, end, err = self.rxfifo[0]
            self.ops.append(("pop",))
            t = self.chip.threads[self.chip.now % isa.NTHREADS]
            t.c, t.n, t.z = end, err, int(word == 0)
            return (i.rd, word)
        if n == "wait":
            c = i.imm
            if c == 0:
                ok = self.cur is None and not self.queue
            elif c == 1:
                ok = self.ev_start
                if ok:
                    self.ops.append(("consume", "start"))
            elif c == 2:
                ok = self.ev_stop
                if ok:
                    self.ops.append(("consume", "stop"))
            else:
                ok = bool(self.rxfifo)
            return True if ok else None
        if n == "stop":
            self.ops.append(("stop",))
            return True
        if n == "stat":
            s = self.status()
            self.ops.append(("clear", s & STICKY))
            return (i.rd, s)
        raise ValueError(n)

    def commit(self):
        """Apply deferred thread operations after clock()."""
        for op in self.ops:
            k = op[0]
            if k == "push":
                self.queue.append(op[1])
            elif k == "pop":
                word, count, end, err = self.rxfifo.popleft()
                self.last_count = count
            elif k == "consume":
                if op[1] == "start" and not self.set_start:
                    self.ev_start = 0
                if op[1] == "stop" and not self.set_stop:
                    self.ev_stop = 0
            elif k == "clear":
                keep = (self.set_start << S_START) | (self.set_stop << S_STOP)
                self.stat &= ~(op[1] & ~keep)
                if op[1] >> S_START & 1 and not self.set_start:
                    self.ev_start = 0
                if op[1] >> S_STOP & 1 and not self.set_stop:
                    self.ev_stop = 0
            elif k == "stop":
                self.cur = None
                self.queue.clear()
                self.rxfifo.clear()
                self.rx_new = []
                self.frozen = False
                self.pending_start = None
        for entry in self.rx_new:
            if len(self.rxfifo) >= RX_DEPTH:
                self.stat |= 1 << S_OVERRUN
            else:
                self.rxfifo.append(entry)
        self.ops = []

    # ------------------------------------------------------------ lane clock
    def read_inputs(self):
        """Filtered {D, I, C, A} levels for this cycle (bit order D=0, I=1, C=2, A=3)."""
        raw_all = self.chip.pins_in
        flt = (self.regs[MODE] >> 9) & 7
        cur = []
        for k, role in enumerate(("d", "i", "c", "a")):
            p = self.pin(role)
            raw = 0 if p == NONE_PIN else (raw_all >> p) & 1
            cur.append(raw if flt == 0 else self.filt[k])
            if flt:
                if raw == self.filt[k]:
                    self.fcnt[k] = 0
                else:
                    self.fcnt[k] += 1
                    if self.fcnt[k] >= flt:
                        self.filt[k] = raw
                        self.fcnt[k] = 0
        return cur

    def edge_offset_cycles(self):
        """Cycles between a pad edge and the cycle after its filtered match."""
        return SYNC_LATENCY + ((self.regs[MODE] >> 9) & 7) + 1

    def clock(self):
        self.set_start = self.set_stop = 0
        self.rx_new = []
        d, i_, c, a = self.read_inputs()
        cur4 = d | i_ << 1 | c << 2 | a << 3
        pat8 = (self.prev4 << 4) | cur4
        prev_i = (self.prev4 >> 1) & 1
        self.prev4 = cur4
        now = self.chip.now
        m_start = self.match(self.regs[PAT], pat8)
        m_stop = self.match(self.regs[PATE], pat8)
        if m_start:
            self.ev_start = self.set_start = 1
            self.stat |= 1 << S_START
            self.regs[CAP] = now
        if m_stop:
            self.ev_stop = self.set_stop = 1
            self.stat |= 1 << S_STOP

        if bit(self.regs[MODE], 7):  # capture mode
            if i_ != prev_i:
                self.rx_new.append((now, 16, 0, i_))
            return

        self._m_start, self._prev_i = m_start, prev_i
        if self.cur is not None:
            self.run_cycle(i_, c, prev_i, m_stop)
        elif self.queue:
            if self.queue[0].kind in ("lw", "start"):
                self.apply_cfg(self.queue.popleft())  # one per cycle while idle
            else:
                self.try_start()

    @staticmethod
    def match(pat, v):
        mask, val = pat >> 8, pat & 0xFF
        return mask != 0 and (v & mask) == (val & mask)

    # One cycle of a running command, at position (t, pre). Afterwards the
    # position is advanced to the next cycle and pin levels for it are set.
    def run_cycle(self, i_, c_in, prev_i, m_stop):
        cmd = self.cur
        if self.frozen:
            if c_in:
                self.frozen = False
            else:
                return
        if self.pre == 0:
            # Stretch: clock released at CR (or at the start of an emit slot)
            if self.cfg(C_STRETCH) and self.cfg(C_COD) and not self.frozen_done:
                release = False
                if self.slot_kind == "emit":
                    release = self.t == 0 and (self.c[1] == 0)
                elif self.cfg(C_CEN):
                    release = self.t == self.regs[CR] and self.c[1] == 0
                if release and not c_in:
                    self.frozen = True
                    self.frozen_done = True
                    return
            if self.slot_sample and self.t == self.regs[SAMP]:
                if self.take_sample(i_):
                    return  # the command ended; a follow-on command starts at t = 0
        # Stop pattern ends a receive early
        if m_stop and cmd.kind == "rx":
            self.push_rx(end=1)
            self.finish_cmd()
            return
        # Resync on data edges during receive (full lanes)
        rs = (self.regs[MODE] >> 5) & 3 if self.full else 0
        if rs and self.slot_sample and cmd.kind == "rx" and i_ != prev_i:
            if rs == 3 or (rs == 1 and i_) or (rs == 2 and not i_):
                if self.rx_complete:
                    # the edge starts the next bit: hand over to the queued continuation
                    self.finish_cmd()
                    if self.cur is None:
                        return
                off = self.edge_offset_cycles()
                self.t, self.pre = off >> self.pre_bits, off & ((1 << self.pre_bits) - 1)
                self.set_levels()
                return
        # advance
        self.pre += 1
        if self.pre >> self.pre_bits:
            self.pre = 0
            self.t += 1
        if self.t >= self.slot_len:
            self.end_slot()
        else:
            self.set_levels()

    def take_sample(self, level):
        """Returns True when this sample completed the receive command."""
        cmd = self.cur
        if self.slot_kind == "tx":  # echo (tx x) and arbitration
            if bit(self.regs[MODE], 8) and self.d[1] == 0 and level == 0 and not self.tx_last:
                if not self.arb_lost_cmd:
                    self.stat |= 1 << S_ARBLOST
                self.arb_lost_cmd = True
        if self.full and self.cfg(C_NRZI):
            x = int(level == self.rline)
            self.rline = level
        else:
            x = level
        stuff = (self.regs[CFG] >> 8) & 7 if self.full else 0
        if stuff:
            if self.destuff:
                self.destuff = False
                if x == self.rlast and (self.cfg(C_STUFFANY) or x == 1):
                    self.rerr = 1
                    self.stat |= 1 << S_STUFFERR
                self.rrun = 1 if self.cfg(C_STUFFANY) else int(x == 1)
                self.rlast = x
                return self.check_rx_complete()
            if self.cfg(C_STUFFANY):
                self.rrun = self.rrun + 1 if x == self.rlast else 1
            else:
                self.rrun = self.rrun + 1 if x else 0
            self.rlast = x
            if self.rrun >= stuff:
                self.destuff = True
        if cmd.kind == "rx" and self.full:
            self.crc_step(x)
        if self.cfg(C_MSB):
            self.rword = ((self.rword << 1) | x) & MASK16
        else:
            self.rword |= x << self.rcount
        self.rcount += 1
        return self.check_rx_complete()

    def check_rx_complete(self):
        cmd = self.cur
        if cmd.kind == "rx" and self.rcount >= cmd.n and not self.destuff:
            self.push_rx(end=0)
            if self.continuation_queued():
                # stay on the bit grid: run to the end of this slot
                self.rx_complete = True
                return False
            self.finish_cmd()
            return True
        return False

    def continuation_queued(self):
        q = self.queue
        if q and q[0].kind == "rx" and q[0].flags & 1:
            return True
        return len(q) >= 2 and q[0].kind in ("lw", "start") and q[1].kind == "rx" and q[1].flags & 1

    def push_rx(self, end):
        self.rx_new.append((self.rword, self.rcount, end, self.rerr))
        self.rword = self.rcount = self.rerr = 0

    def crc_step(self, b):
        crc, poly = self.regs[CRC], self.regs[POLY]
        if self.cfg(C_MSB):
            fb = (crc >> 15) ^ b
            crc = ((crc << 1) & MASK16) ^ (poly if fb else 0)
        else:
            fb = (crc ^ b) & 1
            crc = (crc >> 1) ^ (poly if fb else 0)
        self.regs[CRC] = crc & MASK16

    def end_slot(self):
        cmd = self.cur
        self.prev_level = self.d[0]
        if cmd.kind == "tx" and (self.bits_left > 0 or self.stuff_pending):
            self.begin_tx_slot()
            return
        if cmd.kind == "tx" and cmd.flags & 1:
            self.push_rx(end=0)
        if cmd.kind == "rx" and self.cur is not None and not self.rx_complete:
            # receive keeps sampling until n bits are taken
            self.begin_slot("rx", self.regs[LEN0])
            return
        self.finish_cmd()

    def finish_cmd(self):
        cmd = self.cur
        self.cur = None
        self.frozen = False
        if cmd.kind == "emit":
            self.run = 0
            self.stuff_pending = False
        elif bit(self.regs[MODE], 3) and not any(q.kind in ("tx", "rx", "emit") for q in self.queue):
            self.stat |= 1 << S_UNDER
        # One queued lw/start at a command boundary costs no time.
        if self.queue and self.queue[0].kind in ("lw", "start"):
            self.apply_cfg(self.queue.popleft())
        if self.queue and self.queue[0].kind in ("tx", "rx", "emit"):
            self.try_start()
        if self.cur is None:
            self.hold_levels()

    def apply_cfg(self, q):
        if q.kind == "lw":
            if q.reg == CAP:
                return
            self.regs[q.reg] = self.mask_reg(q.reg, q.data)
            if q.reg == PINS:
                self.owned = {"d": False, "a": False, "c": False}
        else:
            self.pending_start = q.data

    def try_start(self):
        """Head of the queue is a data command and the lane is idle: start it
        in the next cycle if its start condition holds this cycle."""
        head = self.queue[0]
        nxt = (self.chip.now + 1) & MASK16
        if self.pending_start is not None:
            T = self.pending_start
            diff = (nxt - T) & MASK16
            if diff >= 0x8000:
                return  # not yet
            if diff:
                self.stat |= 1 << S_LATE
            self.pending_start = None
            self.begin_cmd(self.queue.popleft())
            return
        if head.kind == "rx" and self.cfg(C_AUTOSTART) and not head.flags & 1:
            if not self._m_start:
                return
            self.begin_cmd(self.queue.popleft(), rline=self._prev_i)
            off = self.edge_offset_cycles()
            self.t, self.pre = off >> self.pre_bits, off & ((1 << self.pre_bits) - 1)
            self.set_levels()
            return
        self.begin_cmd(self.queue.popleft())

    def begin_cmd(self, cmd, rline=None):
        self.cur = cmd
        self.rx_complete = False
        self.arb_lost_cmd = False
        self.frozen_done = False
        if cmd.kind == "emit":
            self.begin_slot("emit", self.regs[LEN0])
        elif cmd.kind == "tx":
            if cmd.flags & 4:  # txcrc: send CRC bits
                crc = self.regs[CRC]
                if bit(self.regs[MODE], 4):
                    crc = ~crc & MASK16
                self.shift = crc >> (16 - cmd.n) if self.cfg(C_MSB) else crc
            else:
                self.shift = cmd.data
            self.bits_left = cmd.n
            self.rword = self.rcount = self.rerr = 0
            self.rline = self.filt_i_now()
            self.begin_tx_slot()
        else:
            self.rword = self.rcount = self.rerr = 0
            # A continuing receive keeps the decoder state (stuffing and NRZI
            # span word boundaries); any other receive starts it fresh.
            if not cmd.flags & 1:
                self.rline = self.filt_i_now() if rline is None else rline
                self.rrun = 0
                self.destuff = False
            self.begin_slot("rx", self.regs[LEN0])

    def filt_i_now(self):
        return (self.prev4 >> 1) & 1

    def begin_tx_slot(self):
        cmd = self.cur
        full = self.full
        stuff = (self.regs[CFG] >> 8) & 7 if full else 0
        if self.stuff_pending:
            x = 1 - self.last_bit
            self.stuff_pending = False
            self.run = 1 if self.cfg(C_STUFFANY) else int(x == 1)
        else:
            n = cmd.n
            k = n - self.bits_left
            x = (self.shift >> (n - 1 - k)) & 1 if self.cfg(C_MSB) else (self.shift >> k) & 1
            self.bits_left -= 1
            if full and cmd.flags & 2 and not cmd.flags & 4:
                self.crc_step(x)
            if stuff:
                if self.cfg(C_STUFFANY):
                    self.run = self.run + 1 if x == self.last_bit else 1
                else:
                    self.run = self.run + 1 if x else 0
                if self.run >= stuff:
                    self.stuff_pending = True
        self.last_bit = x
        self.tx_last = self.bits_left == 0 and not self.stuff_pending
        if full and self.cfg(C_NRZI):
            if x == 0:
                self.line ^= 1
            v = self.line
        else:
            v = x
        self.slot_v = v
        self.begin_slot("tx", self.regs[LEN1] if v else self.regs[LEN0])

    def begin_slot(self, kind, length):
        self.slot_kind = kind
        self.slot_sample = kind == "rx" or (kind == "tx" and self.cur.flags & 1)
        self.acc += self.regs[FRAC]
        extra = self.acc >> 8
        self.acc &= 0xFF
        self.slot_len = max(length, 1) + extra
        self.t = self.pre = 0
        self.frozen_done = False
        if kind == "emit":
            w = self.cur.data
            for role, lb, eb in (("d", 0, 4), ("a", 1, 5), ("c", 2, 6)):
                lvl, en = bit(w, lb), bit(w, eb)
                od = (role == "d" and self.cfg(C_DOD)) or (role == "c" and self.cfg(C_COD))
                if od and lvl:
                    en = 0
                setattr(self, role, [lvl if not od else 0, en])
                if en or od:
                    self.owned[role] = True
            self.line = bit(w, 0)
        self.set_levels()

    def set_levels(self):
        """Pin levels for the cycle at the current position."""
        kind = self.slot_kind
        t = self.t
        if kind == "tx":
            v = self.slot_v
            sym = self.regs[SYM1 if v else SYM0]
            lvl = bit(sym, 14) if t < (sym & 0x1FFF) else bit(sym, 15)
            if bit(sym, 13) and self.full:
                lvl ^= self.prev_level
            if self.arb_lost_cmd:
                lvl = 1
            if self.cfg(C_DOD):
                self.d = [0, 1 - lvl]
            else:
                self.d = [lvl, 1]
            self.owned["d"] = True
            if self.cfg(C_DIFF):
                self.a = [1 - lvl, 1]
                self.owned["a"] = True
        elif kind == "rx" and self.cfg(C_DREL):
            self.d = [self.d[0], 0]
        if kind in ("tx", "rx") and self.cfg(C_CEN):
            cl = self.cfg(C_CPOL) ^ int(self.regs[CR] <= t < self.regs[CF])
            if self.cfg(C_COD):
                self.c = [0, 1 - cl]
            else:
                self.c = [cl, 1]
            self.owned["c"] = True

    def hold_levels(self):
        """Idle: D holds, or is released with DREL; the clock returns to idle."""
        if self.cfg(C_DREL):
            self.d = [self.d[0], 0]
        if self.cfg(C_CEN) and self.owned["c"]:
            cl = self.cfg(C_CPOL)
            self.c = [0, 1 - cl] if self.cfg(C_COD) else [cl, 1]
