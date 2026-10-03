# SPDX-License-Identifier: Apache-2.0
"""SWD debug probe: attach to an Arm MCU the way a debug probe does.

Runs the "swd" firmware image (fw/swd.s). Protocol details follow Arm
ADIv5.2 (IHI 0031G) chapters B2, B4, C2 and D1, summarised in
docs/specs.md section 5.
"""

from .images import IMAGES
from .link import TempoError
from . import jep106

OK, WAIT, FAULT = 1, 2, 4
NOPIN = 15
DP_DPIDR, DP_ABORT, DP_CTRL, DP_SELECT, DP_RDBUFF = 0x0, 0x0, 0x4, 0x8, 0xC
AP_CSW, AP_TAR, AP_DRW, AP_BASE, AP_IDR = 0x00, 0x04, 0x0C, 0xF8, 0xFC
CSYSPWRUPACK, CSYSPWRUPREQ, CDBGPWRUPACK, CDBGPWRUPREQ = 1 << 31, 1 << 30, 1 << 29, 1 << 28


class SwdError(TempoError):
    pass


class SwdFault(SwdError):
    pass


def request(apndp, rnw, addr):
    """8-bit request, LSB first on the wire: start, APnDP, RnW, A[2], A[3],
    parity, stop, park."""
    a2, a3 = (addr >> 2) & 1, (addr >> 3) & 1
    par = apndp ^ rnw ^ a2 ^ a3
    return 1 | apndp << 1 | rnw << 2 | a2 << 3 | a3 << 4 | par << 5 | 0 << 6 | 1 << 7


class Swd:
    def __init__(self, link, swdio=0, swclk=1, period=20, retries=20):
        """swdio, swclk: Tempo pin indices. period: cycles per bit
        (SWCLK = link.clock_hz / period)."""
        self.link = link
        self.retries = retries
        half = period // 2
        link.load(IMAGES["swd"], {
            "pins": NOPIN << 12 | swclk << 8 | swdio << 4 | swdio,
            "per": period, "cr": half, "cf": period, "samp": half - 1,
        })
        self.select = None
        self.waits = 0

    # ---- raw sequences
    def seq(self, bits, n):
        words = [0x1000 | n]
        for i in range(0, n, 16):
            words.append((bits >> i) & 0xFFFF)
        self.link.put(words)

    def line_reset(self):
        """At least 50 cycles with SWDIO high, then idle cycles (B4.3.3)."""
        self.seq((1 << 56) - 1, 56)
        self.seq(0, 8)

    def jtag_to_swd(self):
        """Select SWD on an SWJ-DP that powered up in JTAG (B5.2.2)."""
        self.seq((1 << 56) - 1, 56)
        self.seq(0xE79E, 16)
        self.line_reset()

    # ---- transactions with WAIT retry
    def _xfer(self, apndp, rnw, addr, data=0):
        req = request(apndp, rnw, addr)
        for _ in range(self.retries):
            if rnw:
                self.link.put([0x2000 | req])
                ack = self.link.get(1)[0]
                if ack & 7 == OK:
                    lo, hi = self.link.get(2)
                    if ack & 0x100:
                        raise SwdError("read data parity error")
                    return lo | hi << 16
            else:
                self.link.put([0x3000 | req, data & 0xFFFF, data >> 16])
                ack = self.link.get(1)[0]
                if ack & 7 == OK:
                    return None
            ack &= 7
            if ack == WAIT:
                self.waits += 1
                continue
            if ack == FAULT:
                raise SwdFault("FAULT on %s %s 0x%x" % ("AP" if apndp else "DP",
                                                       "read" if rnw else "write", addr))
            raise SwdError("no valid ACK (%d): target not responding or wrong wiring" % ack)
        raise SwdError("WAIT %d times on %s 0x%x" % (self.retries, "AP" if apndp else "DP", addr))

    def dp_read(self, addr):
        return self._xfer(0, 1, addr)

    def dp_write(self, addr, value):
        self._xfer(0, 0, addr, value)

    def _select(self, apsel, reg):
        sel = apsel << 24 | (reg & 0xF0)
        if sel != self.select:
            self.dp_write(DP_SELECT, sel)
            self.select = sel

    def ap_read(self, apsel, reg):
        """AP reads are posted (B4.2.2): issue the read, then collect the
        result from RDBUFF."""
        self._select(apsel, reg)
        self._xfer(1, 1, reg & 0xC)
        return self.dp_read(DP_RDBUFF)

    def ap_write(self, apsel, reg, value):
        self._select(apsel, reg)
        self._xfer(1, 0, reg & 0xC, value)

    def clear_errors(self):
        """ABORT with STKCMPCLR, STKERRCLR, WDERRCLR, ORUNERRCLR."""
        self.dp_write(DP_ABORT, 0x1E)

    # ---- attach
    def connect(self, from_jtag=True):
        """Line reset (after the JTAG-to-SWD switch if needed) and DPIDR read.
        Returns a dict describing the debug port."""
        self.select = None
        if from_jtag:
            self.jtag_to_swd()
        else:
            self.line_reset()
        idr = self.dp_read(DP_DPIDR)
        if not idr & 1:
            raise SwdError("DPIDR 0x%08x has bit 0 clear: not a DP" % idr)
        return {"dpidr": idr, "designer": jep106.name((idr >> 1) & 0x7FF),
                "version": (idr >> 12) & 0xF, "partno": (idr >> 20) & 0xFF,
                "revision": idr >> 28}

    def power_up(self, timeout_us=20000):
        """Request debug and system power, wait for both ACKs (B2.2.2)."""
        self.dp_write(DP_CTRL, CSYSPWRUPREQ | CDBGPWRUPREQ)
        t = self.link.t
        t0 = t.ticks_us()
        while True:
            st = self.dp_read(DP_CTRL)
            if st & CSYSPWRUPACK and st & CDBGPWRUPACK:
                return st
            if t.ticks_us() - t0 > timeout_us:
                raise SwdError("power-up ACK missing: CTRL/STAT 0x%08x" % st)
            t.sleep_us(50)

    def mem_read(self, addr, apsel=0):
        self.ap_write(apsel, AP_CSW, 0x23000002)  # 32-bit, no increment
        self.ap_write(apsel, AP_TAR, addr)
        return self.ap_read(apsel, AP_DRW)

    def rom_table(self, apsel=0, max_entries=32):
        """MEM-AP IDR and BASE, then the ROM table's entries, each with its
        component class (D1, docs/specs.md 5.3)."""
        idr = self.ap_read(apsel, AP_IDR)
        base = self.ap_read(apsel, AP_BASE)
        if base == 0xFFFFFFFF or not base & 1:
            return {"idr": idr, "base": None, "entries": []}
        rom = base & 0xFFFFF000
        entries = []
        for i in range(max_entries):
            e = self.mem_read(rom + 4 * i, apsel)
            if e == 0:
                break
            if e & 1:
                off = e >> 12
                if off & 0x80000:
                    off -= 0x100000
                comp = (rom + (off << 12)) & 0xFFFFFFFF
                cidr1 = self.mem_read(comp + 0xFF4, apsel)
                entries.append({"address": comp, "class": (cidr1 >> 4) & 0xF})
        cid = [self.mem_read(rom + 0xFF0 + 4 * k, apsel) & 0xFF for k in range(4)]
        return {"idr": idr, "base": rom, "rom_cid_ok": cid[0] == 0x0D and cid[2] == 0x05 and cid[3] == 0xB1,
                "rom_class": cid[1] >> 4, "entries": entries}
