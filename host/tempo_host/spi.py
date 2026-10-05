# SPDX-License-Identifier: Apache-2.0
"""SPI master and SPI NOR flash programmer.

Runs the "spi" firmware image (fw/spi.s) on lane 2. Flash commands follow
the JEDEC-style command set of parts like the W25Q128JV; SFDP parsing follows JESD216 as implemented in Linux
drivers/mtd/spi-nor/sfdp.c.
"""

from .images import IMAGES
from .link import TempoError

OP_WRITE, OP_READ, OP_END, OP_XFER, OP_BULK = 1, 2, 3, 4, 5
BULK_HALF = 256  # words per buffer half
MAX_N = 4094  # bytes per firmware command (even, fits the 12-bit count)
NOPIN = 15
E_D, E_A, E_C, E_DEN, E_AEN, E_CEN = 1, 2, 4, 16, 32, 64
CFG_MSB, CFG_CEN, CFG_CPOL = 1, 8, 16


def _pack(data, msb_first=True):
    data = bytes(data)
    out = []
    for i in range(0, len(data), 2):
        hi = data[i]
        lo = data[i + 1] if i + 1 < len(data) else 0
        # Firmware handles an odd byte in the high half in either bit order.
        out.append(hi << 8 | lo if msb_first or i + 1 == len(data) else lo << 8 | hi)
    return out


def _unpack(words, n, msb_first=True):
    out = bytearray()
    for w in words:
        if msb_first or len(out) + 1 == n:
            out.append(w >> 8)
            out.append(w & 0xFF)
        else:
            out.append(w & 0xFF)
            out.append(w >> 8)
    return bytes(out[:n])


class Spi:
    def __init__(self, link, mosi=0, miso=1, sck=2, cs=3, mode=0, period=8, sample=None,
                 msb_first=True):
        """Pins are Tempo pin indices (0-7 = uio0-7). period: clock cycles per
        bit (SCK = clock / period), 6..8191 for modes 0/2, 7..8191 for
        modes 1/3. sample: cycle within the bit
        at which MISO is sampled, default period - 1 (latest point, the most
        margin for pad and target delays)."""
        if mode not in (0, 1, 2, 3):
            raise ValueError("SPI mode must be 0..3")
        cpol, cpha = mode >> 1, mode & 1
        # Registered pads, target response and the two input synchroniser
        # stages and the lane input register delay MISO. Modes 1/3 also shift on this slot's first edge.
        # These minima are checked against a target on the cycle-level board;
        # real pad/target delays can require a longer period.
        earliest = 5 + cpha
        if not earliest + 1 <= period <= 8191:
            raise ValueError("period must be %d..8191 cycles for this mode" % (earliest + 1))
        sample = period - 1 if sample is None else sample
        if not earliest <= sample < period:
            raise ValueError("sample must be %d..period-1" % earliest)
        self.link = link
        self.msb_first = msb_first
        half = period // 2
        # CPHA 0: the shifting edge sits one cycle before the slot end, so
        # MISO has a whole period to come back whether period is odd or even.
        cr = 0 if cpha else period - 1 - half
        idle_c = E_C if cpol else 0
        cs_low = E_DEN | E_AEN | E_CEN | idle_c
        self.period = period
        link.load(IMAGES["spi"], {
            "pins": cs << 12 | sck << 8 | miso << 4 | mosi,
            "per": period, "cr": cr, "cf": cr + half,
            "samp": sample,
            "cfg": (CFG_MSB if msb_first else 0) | CFG_CEN | (CFG_CPOL if cpol else 0),
            "cslow": cs_low, "cshigh": cs_low | E_A,
        })

    @property
    def sck_hz(self):
        return self.link.clock_hz / self.period

    def write(self, data):
        data = bytes(data)
        for i in range(0, len(data), MAX_N):
            part = data[i:i + MAX_N]
            self.link.put([OP_WRITE << 12 | len(part)] + _pack(part, self.msb_first))

    def read(self, n, sink=None):
        """Clock n bytes in (MOSI low). With sink, each chunk is passed to
        sink(bytes) instead of being collected (for long dumps)."""
        out = bytearray()
        left = n
        while left:
            k = min(left, MAX_N)
            self.link.put([OP_READ << 12 | k])
            chunk = _unpack(self.link.get((k + 1) // 2), k, self.msb_first)
            if sink:
                sink(chunk)
            else:
                out += chunk
            left -= k
        return bytes(out)

    def read_bulk(self, n, sink=None):
        """Clock n bytes in (n a multiple of 512) through the SRAM double
        buffer: one IMEM read transaction per 512 bytes instead of one
        mailbox transaction per 8."""
        if n % 512:
            raise ValueError("bulk reads move whole 512-byte blocks")
        link = self.link
        base = link.param_values["buf"]
        out = bytearray()
        left = n // 512
        while left:
            k = min(left, 4095)
            link.put([OP_BULK << 12 | k])
            for _ in range(k):
                half = link.get(1)[0]
                chunk = _unpack(link.read_imem(base + BULK_HALF * half, BULK_HALF),
                                2 * BULK_HALF, self.msb_first)
                link.put([0])  # this half is free again
                if sink:
                    sink(chunk)
                else:
                    out += chunk
            left -= k
        return bytes(out)

    def xfer(self, data):
        data = bytes(data)
        out = bytearray()
        for i in range(0, len(data), 8):
            part = data[i:i + 8]
            self.link.put([OP_XFER << 12 | len(part)] + _pack(part, self.msb_first))
            out += _unpack(self.link.get((len(part) + 1) // 2), len(part), self.msb_first)
        return bytes(out)

    def end(self):
        self.link.put([OP_END << 12])

    def transaction(self, write=b"", read=0):
        """CS low, write, read, CS high."""
        if write:
            self.write(write)
        data = self.read(read) if read else b""
        self.end()
        return data


class FlashError(TempoError):
    pass


class SpiFlash:
    """SPI NOR flash on an Spi bus."""

    def __init__(self, spi, busy_timeout_us=500000):
        self.spi = spi
        self.busy_timeout = busy_timeout_us
        self.size = None

    def jedec_id(self):
        """(manufacturer, memory type, capacity) from 0x9F."""
        return tuple(self.spi.transaction(b"\x9f", 3))

    def status(self):
        return self.spi.transaction(b"\x05", 1)[0]

    def sfdp(self, addr, n):
        return self.spi.transaction(bytes([0x5A, 0, addr >> 8, addr & 0xFF, 0]), n)

    def sfdp_info(self):
        """Parse the SFDP header and the Basic Flash Parameter Table.
        Returns a dict with revision, size in bytes and the 4 KB erase
        opcode, or None if there is no SFDP signature."""
        hdr = self.sfdp(0, 16)
        if hdr[0:4] != b"SFDP":
            return None
        ptr = hdr[12] | hdr[13] << 8 | hdr[14] << 16
        dwords = hdr[11]
        if (hdr[15] << 8 | hdr[8]) != 0xFF00 or dwords < 2:
            raise FlashError("first SFDP parameter header is not the BFPT")
        bfpt = self.sfdp(ptr, 4 * min(dwords, 9))
        d1 = int.from_bytes(bfpt[0:4], "little")
        d2 = int.from_bytes(bfpt[4:8], "little")
        bits = (1 << (d2 & 0x7FFFFFFF)) if d2 >> 31 else d2 + 1
        self.size = bits // 8
        return {"rev": (hdr[5], hdr[4]), "size": self.size, "erase_4k_opcode": (d1 >> 8) & 0xFF}

    def read(self, addr, n, sink=None):
        self.spi.write(bytes([0x03, addr >> 16 & 0xFF, addr >> 8 & 0xFF, addr & 0xFF]))
        data = self.spi.read(n, sink)
        self.spi.end()
        return data

    def wait_ready(self):
        t = self.spi.link.t
        t0 = t.ticks_us()
        while self.status() & 1:
            if t.ticks_us() - t0 > self.busy_timeout:
                raise FlashError("flash still busy after %d us" % self.busy_timeout)
            t.sleep_us(20)

    def write_enable(self):
        self.spi.transaction(b"\x06")
        if not self.status() & 2:
            raise FlashError("WEL did not set after WREN (write protected?)")

    def erase_sector(self, addr):
        """Erase the 4 KB sector holding addr."""
        self.write_enable()
        self.spi.transaction(bytes([0x20, addr >> 16 & 0xFF, addr >> 8 & 0xFF, addr & 0xFF]))
        self.wait_ready()

    def program(self, addr, data, verify=True):
        """Page-program data at addr, split at 256-byte page boundaries so
        the part's in-page wrap never triggers. Verifies by read-back."""
        data = bytes(data)
        i = 0
        while i < len(data):
            room = 256 - ((addr + i) & 0xFF)
            chunk = data[i:i + room]
            a = addr + i
            self.write_enable()
            self.spi.transaction(bytes([0x02, a >> 16 & 0xFF, a >> 8 & 0xFF, a & 0xFF]) + chunk)
            self.wait_ready()
            i += len(chunk)
        if verify and self.read(addr, len(data)) != data:
            raise FlashError("verify failed at 0x%06x" % addr)
