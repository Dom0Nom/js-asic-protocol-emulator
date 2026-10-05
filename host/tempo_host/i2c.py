# SPDX-License-Identifier: Apache-2.0
"""I2C master: bus scan, register access, 24Cxx EEPROMs, bus recovery.

Runs the "i2c" firmware image (fw/i2c.s) on lane 2. Timing per NXP UM10204
Table 11: the bit slot is split so that SCL low and high times
meet tLOW/tHIGH of the chosen mode (slots scale with the link's clock).
"""

from .images import IMAGES
from .link import TempoError

START, STOP, WRITE, READ_ACK, READ_NACK, PROBE, RECOVER = 1, 2, 3, 4, 5, 6, 7
NOPIN = 15

# name: bit slot in cycles at a 50 MHz reference clock; I2c scales it to
# link.clock_hz (125 -> 63 cycles, about 397 kHz, at 25 MHz)
MODES = {
    "sm": 500,    # 100 kHz
    "fm": 125,    # 400 kHz
    "fmp": 50,    # 1 MHz
}


class I2cError(TempoError):
    pass


class Nack(I2cError):
    pass


def timing(slot):
    """(slot, cr, cf, samp) with SCL high from 44% to 88% of the slot."""
    cr = slot * 44 // 100
    cf = slot * 88 // 100
    return slot, cr, cf, (cr + cf) // 2 + 4


class I2c:
    def __init__(self, link, sda=4, scl=5, mode="fm", slot=None, filter_cycles=0):
        """sda, scl: Tempo pin indices (0-7 = uio0-7). mode: sm, fm, fmp;
        slot overrides the bit time in clock cycles."""
        self.link = link
        # round up so the bus never runs faster than the mode's nominal rate
        s, cr, cf, samp = timing(slot or -(-MODES[mode] * link.clock_hz // 50000000))
        self.slot = s
        link.load(IMAGES["i2c"], {
            "pins": NOPIN << 12 | scl << 8 | sda << 4 | sda,
            "slot": s, "cr": cr, "cf": cf, "samp": samp,
            "mode": (filter_cycles & 7) << 9,
            "sdamask": 1 << sda,
        })

    def _cmd(self, words, replies):
        self.link.put(words)
        return self.link.get(replies) if replies else []

    # ---- primitives
    def probe(self, addr, read=False):
        """True if a target ACKs addr (7-bit). A read probe reads and NACKs
        one byte; a write probe sends no data (a zero-length write)."""
        return self._cmd([PROBE << 8 | (addr << 1) | int(read)], 1)[0] == 0

    def scan(self, first=0x08, last=0x77, read_ranges=((0x30, 0x37), (0x50, 0x5F))):
        """7-bit addresses that ACK. Skips the reserved addresses 0x00-0x07 and
        0x78-0x7F by default. Like i2cdetect, probes EEPROM-like ranges with a
        read so no write cycle can start."""
        found = []
        for a in range(first, last + 1):
            rd = any(lo <= a <= hi for lo, hi in read_ranges)
            if self.probe(a, rd):
                found.append(a)
        self.link.check()
        return found

    def recover(self):
        """Clock SCL until a target holding SDA low lets go (at most 9
        pulses), then STOP. Returns the number of pulses used."""
        r = self._cmd([RECOVER << 8], 1)[0]
        if r & 0x10:
            raise I2cError("SDA still low after 9 SCL pulses")
        return r & 0xF

    def transfer(self, addr, write=b"", read=0, addr10=False):
        """One combined transaction: START, write bytes, repeated START,
        read bytes (last one NACKed), STOP. Raises Nack on a NACKed address
        or data byte (after sending STOP)."""
        words = [START << 8]
        if addr10:
            # UM10204 3.1.11: 11110 A9 A8 W, A7..A0; a read then repeats
            # the first byte with R after a repeated START
            hi = 0xF0 | ((addr >> 7) & 6)
            words += [WRITE << 8 | hi, WRITE << 8 | (addr & 0xFF)]
            words += [WRITE << 8 | b for b in write]
            if read:
                words += [START << 8, WRITE << 8 | hi | 1]
        else:
            if write or not read:
                words += [WRITE << 8 | (addr << 1)] + [WRITE << 8 | b for b in write]
                if read:
                    words.append(START << 8)
            if read:
                words.append(WRITE << 8 | (addr << 1) | 1)
        if read:
            words += [READ_ACK << 8] * (read - 1) + [READ_NACK << 8]
        # send in pieces, collecting replies; stop at the first NACK
        replies = []
        acks = []
        i = 0
        while i < len(words):
            # A WRITE can NACK, including either 10-bit address byte and
            # the read address after a repeated START. Await that ACK
            # before queuing anything after it. Reads can still be batched.
            end = i
            while end < len(words) and end - i < 4:
                end += 1
                if (words[end - 1] >> 8) == WRITE:
                    break
            part = words[i:end]
            i += len(part)
            want = sum(1 for w in part if (w >> 8) in (WRITE, READ_ACK, READ_NACK))
            got = self._cmd(part, want)
            for w, r in zip([w for w in part if (w >> 8) in (WRITE, READ_ACK, READ_NACK)], got):
                if (w >> 8) == WRITE:
                    acks.append(r)
                    if r:
                        self._cmd([STOP << 8], 0)
                        raise Nack("NACK after %d byte(s) to 0x%02x" % (len(acks), addr))
                else:
                    replies.append(r)
        self._cmd([STOP << 8], 0)
        self.link.check()
        return bytes(replies)

    # ---- register devices
    def read_regs(self, addr, reg, n):
        return self.transfer(addr, bytes([reg]), n)

    def write_regs(self, addr, reg, data):
        self.transfer(addr, bytes([reg]) + bytes(data))

    def dump(self, addr, n=256):
        out = bytearray()
        for base in range(0, n, 16):
            out += self.read_regs(addr, base, min(16, n - base))
        return bytes(out)


class Eeprom24:
    """24Cxx serial EEPROM. Page size and address width from the part
    number (Microchip AT24C02C, 24LC256 datasheets)."""

    PARTS = {
        # part: (size bytes, page bytes, address bytes)
        "24c01": (128, 8, 1), "24c02": (256, 8, 1),
        "24c04": (512, 16, 1), "24c08": (1024, 16, 1), "24c16": (2048, 16, 1),
        "24c32": (4096, 32, 2), "24c64": (8192, 32, 2),
        "24c128": (16384, 64, 2), "24c256": (32768, 64, 2), "24c512": (65536, 128, 2),
    }

    def __init__(self, i2c, part="24c02", addr=0x50, write_timeout_us=10000):
        self.i2c = i2c
        self.size, self.page, self.abytes = self.PARTS[part.lower()]
        self.addr = addr
        self.timeout = write_timeout_us

    def _dev(self, mem):
        # 24C04/08/16 put memory address bits above 8 into the device address
        if self.abytes == 1:
            return self.addr | ((mem >> 8) & 7), bytes([mem & 0xFF])
        return self.addr, bytes([mem >> 8, mem & 0xFF])

    def read(self, mem, n):
        out = bytearray()
        while n:
            # 1-byte parts: a sequential read does not cross into the next block address
            chunk = min(n, 256 - (mem & 0xFF)) if self.abytes == 1 else n
            chunk = min(chunk, 64)
            dev, a = self._dev(mem)
            out += self.i2c.transfer(dev, a, chunk)
            mem += chunk
            n -= chunk
        return bytes(out)

    def write(self, mem, data):
        """Page writes never cross a page boundary (the address counter
        rolls over within the page). Waits for each write cycle by ACK
        polling."""
        data = bytes(data)
        i = 0
        while i < len(data):
            room = self.page - ((mem + i) % self.page)
            chunk = data[i:i + room]
            dev, a = self._dev(mem + i)
            self.i2c.transfer(dev, a + chunk)
            self.wait_ready(dev)
            i += len(chunk)

    def wait_ready(self, dev=None):
        """ACK polling: the part NACKs its address during the write cycle."""
        link = self.i2c.link
        t0 = link.t.ticks_us()
        while not self.i2c.probe(self.addr if dev is None else dev):
            if link.t.ticks_us() - t0 > self.timeout:
                raise I2cError("EEPROM still busy after %d us" % self.timeout)
