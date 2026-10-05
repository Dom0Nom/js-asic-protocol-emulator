"""SWD target model: an Arm SW-DP with one MEM-AP and a memory map.

Wire behaviour per Arm ADIv5.2 (IHI 0031G) chapter B4: the target samples SWDIO on the rising edge of
SWCLK and changes (or releases) SWDIO on the rising edge. After the 8-bit
request there is one turnaround cycle, then the target drives the 3-bit ACK
(LSB first: OK 0b001, WAIT 0b010, FAULT 0b100). A read continues with 32
data bits and even parity, then a turnaround; a write has a turnaround
after the ACK, then the host sends 32 data bits and parity.

Registers (DPv1): DPIDR/ABORT at 0x0, CTRL/STAT 0x4, SELECT 0x8, RDBUFF 0xC.
AP reads are posted: an AP read returns the result of the previous AP read,
and RDBUFF returns the last one. MEM-AP: CSW 0x00, TAR 0x04, DRW 0x0C,
BASE 0xF8, IDR 0xFC.

Error injection: `wait_count` makes the first N AP accesses answer WAIT;
`fault_addrs` makes a DRW access to those memory addresses set STICKYERR,
after which AP accesses answer FAULT until ABORT.STKERRCLR is written.
Violations of the protocol by the host are listed in `violations`.
"""

JTAG_TO_SWD = 0xE79E  # 16 bits, LSB first (ADIv5.2 B5.2.2)

CSYSPWRUPACK, CSYSPWRUPREQ = 1 << 31, 1 << 30
CDBGPWRUPACK, CDBGPWRUPREQ = 1 << 29, 1 << 28
STICKYERR = 1 << 5


def rom_table_memory(rom_base=0xE00FF000, components=(0xE000E000, 0xE0001000)):
    """Words of a Class 0x1 ROM table listing `components`, each with a
    component ID block (CIDR0-3 at 0xFF0-0xFFC)."""
    mem = {}
    for i, c in enumerate(components):
        off = ((c - rom_base) >> 12) & 0xFFFFF
        mem[rom_base + 4 * i] = off << 12 | 0b11  # FORMAT = 32-bit, PRESENT
    mem[rom_base + 4 * len(components)] = 0  # end of table

    def cid(base, cls):
        for k, v in enumerate((0x0D, cls << 4, 0x05, 0xB1)):
            mem[base + 0xFF0 + 4 * k] = v
    cid(rom_base, 0x1)
    for c in components:
        cid(c, 0xE)  # generic IP
    return mem


class SwdTarget:
    def __init__(self, swclk, swdio, dpidr=0x2BA01477, idr=0x24770011, base=0xE00FF000,
                 memory=None, start_in_jtag=True, wait_count=0, fault_addrs=(), pwrup_delay=0):
        self.swclk, self.swdio = swclk, swdio
        self.dpidr, self.idr, self.base = dpidr, idr, base
        self.mem = dict(rom_table_memory(base)) if memory is None else dict(memory)
        self.jtag = start_in_jtag
        self.wait_left = wait_count
        self.fault_addrs = set(fault_addrs)
        self.pwrup_delay = pwrup_delay
        self.ctrl = 0
        self.select = 0
        self.rdbuff = 0
        self.csw = 0x03000042
        self.tar = 0
        self.violations = []
        self.protocol_errors = 0
        self.transactions = []  # (kind, addr, value, ack)
        self._prev_clk = 0
        self._ones = 0
        self._hist = 0  # last 16 sampled bits, newest in bit 15 (LSB-first order)
        self._state = "reset"  # reset: needs a line reset (and DPIDR read)
        self._req = []
        self._drive = None  # level driven on SWDIO, None = released
        self._sched = []  # levels to drive at the next rising edges
        self._recv = 0  # bits to receive (write data phase)
        self._skip = 0  # edges to wait before receiving
        self._bits = []
        self._pending_write = None
        self._pwr_cycles = 0
        self.cycle = 0

    # ---- helpers
    def _violation(self, what):
        self.violations.append((self.cycle, what))

    def _ack(self, is_ap):
        if is_ap and self.wait_left:
            self.wait_left -= 1
            return 2
        if is_ap and (self.ctrl & STICKYERR or not self.ctrl & CDBGPWRUPACK):
            if not self.ctrl & CDBGPWRUPACK:
                self.ctrl |= STICKYERR
            return 4
        return 1

    def _mem_read(self, a):
        if a in self.fault_addrs:
            self.ctrl |= STICKYERR
            return 0
        return self.mem.get(a & ~3, 0)

    def _ap_read(self, reg):
        if reg == 0x00:
            return self.csw
        if reg == 0x04:
            return self.tar
        if reg == 0x0C:
            v = self._mem_read(self.tar)
            if (self.csw >> 4) & 3 == 1:
                self.tar = (self.tar + 4) & 0xFFFFFFFF
            return v
        if reg == 0xF8:
            return self.base | 0b11
        if reg == 0xFC:
            return self.idr
        return 0

    def _ap_write(self, reg, v):
        if reg == 0x00:
            self.csw = (self.csw & ~0x3F) | (v & 0x3F)
        elif reg == 0x04:
            self.tar = v
        elif reg == 0x0C:
            if self.tar in self.fault_addrs:
                self.ctrl |= STICKYERR
            else:
                self.mem[self.tar & ~3] = v
            if (self.csw >> 4) & 3 == 1:
                self.tar = (self.tar + 4) & 0xFFFFFFFF

    def _dp_write(self, a, v):
        if a == 0x0:  # ABORT
            if v & (1 << 2):
                self.ctrl &= ~STICKYERR
        elif a == 0x4:
            self.ctrl = (self.ctrl & (CSYSPWRUPACK | CDBGPWRUPACK | STICKYERR)) | (v & 0x5C000000)
            self._pwr_cycles = self.pwrup_delay
        elif a == 0x8:
            self.select = v

    # ---- request handling at the park bit
    def _request(self, bits):
        start, apndp, rnw, a2, a3, par, stop, park = bits
        if start != 1 or stop != 0 or park != 1 or (apndp ^ rnw ^ a2 ^ a3) != par:
            # a real SW-DP ignores it (protocol error, no response); ones
            # inside a line reset land here too, so this is not a violation
            self.protocol_errors += 1
            return
        a = (a3 << 3) | (a2 << 2)
        if self._state == "reset" and not (apndp == 0 and rnw == 1 and a == 0):
            self._violation("first request after line reset is not a DPIDR read")
            return
        self._state = "ready"
        ack = self._ack(apndp)
        ack_bits = [(ack >> i) & 1 for i in range(3)]
        # the schedule drives one level per rising edge; the last entry
        # releases SWDIO, then the host's turnaround cycle is skipped
        self._skip = 1
        if ack != 1:
            self.transactions.append(("ap" if apndp else "dp", a, None, ack))
            self._sched = ack_bits + [None]
            return
        if rnw:
            if apndp:
                reg = (self.select & 0xF0) | a
                v, self.rdbuff = self.rdbuff, self._ap_read(reg)
            elif a == 0x0:
                v = self.dpidr
            elif a == 0x4:
                v = self.ctrl
            elif a == 0xC:
                v = self.rdbuff
            else:
                v = 0
            self.transactions.append(("ap" if apndp else "dp", a, v, ack))
            data = [(v >> i) & 1 for i in range(32)]
            self._sched = ack_bits + data + [sum(data) & 1, None]
        else:
            self._sched = ack_bits + [None]
            self._recv = 33
            self._bits = []
            self._pending_write = (apndp, a)

    def _finish_write(self):
        data = sum(b << i for i, b in enumerate(self._bits[:32]))
        if (sum(self._bits[:32]) & 1) != self._bits[32]:
            self._violation("write data parity")
            self.ctrl |= STICKYERR
            return
        apndp, a = self._pending_write
        self.transactions.append(("ap" if apndp else "dp", a, data, 1))
        if apndp:
            self._ap_write((self.select & 0xF0) | a, data)
        else:
            self._dp_write(a, data)

    # ---- board device
    def step(self, cycle, nets):
        self.cycle = cycle
        if self._pwr_cycles:
            self._pwr_cycles -= 1
        elif self.ctrl & CDBGPWRUPREQ:
            self.ctrl |= CDBGPWRUPACK | (CSYSPWRUPACK if self.ctrl & CSYSPWRUPREQ else 0)
        clk = nets.get(self.swclk, 0)
        rising = clk and not self._prev_clk
        self._prev_clk = clk
        if rising:
            self._edge(nets.get(self.swdio, 1))
        return {self.swdio: self._drive}

    def _edge(self, bit):
        if self._sched:
            self._drive = self._sched.pop(0)
            return
        if self._skip:
            self._skip -= 1
            self._drive = None
            return
        if self._recv:
            self._bits.append(bit)
            self._recv -= 1
            if not self._recv:
                self._finish_write()
            return
        self._drive = None
        # host-driven bits: line reset, JTAG-to-SWD, requests
        self._ones = self._ones + 1 if bit else 0
        self._hist = (self._hist >> 1) | (bit << 15)
        if self.jtag:
            if self._hist == JTAG_TO_SWD:
                self.jtag = False
            return
        if self._ones >= 50:
            self._state = "reset"
            self._req = []
            return
        if self._req or bit:
            self._req.append(bit)
            if len(self._req) == 8:
                bits, self._req = self._req, []
                self._request(bits)
