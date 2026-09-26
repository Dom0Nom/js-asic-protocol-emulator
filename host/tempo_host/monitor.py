# SPDX-License-Identifier: Apache-2.0
"""Passive bus monitors: I2C, SPI and UART traffic with timestamps.

The chip never drives a monitored pin. The I2C and SPI monitors use the
lanes' external-clock receive mode (bits sampled on the bus's own clock
edges); the UART monitor uses two ordinary receive lanes. Records arrive
through an SRAM ring in the chip and are decoded here. Times are in clock
cycles (20 ns at 50 MHz) from the first timed record.
"""

from .images import IMAGES

NOPIN = 15
CLOCK_HZ = 50000000


class _Clock:
    """Unwraps 16-bit chip timestamps; consecutive timed records are less
    than 32768 cycles apart (the firmware adds markers)."""

    def __init__(self):
        self.prev = None
        self.full = 0

    def __call__(self, t):
        if self.prev is None:
            self.prev = t
            return 0
        self.full += (t - self.prev) & 0xFFFF
        self.prev = t
        return self.full


class I2cMonitor:
    def __init__(self, link, sda=4, scl=5, filter_cycles=0):
        self.link = link
        link.load(IMAGES["i2cmon"], {
            "pins": NOPIN << 12 | scl << 8 | sda << 4 | NOPIN,
            "mode": (filter_cycles & 7) << 9,
        })
        self.clock = _Clock()
        self.now = None
        self.lost = 0
        self._pend = None

    def poll(self, max_words=64):
        """Decode whatever the chip has sent: a list of events
        ("start", t), ("byte", value, ack), ("stop",), ("lost", n)."""
        out = []
        for _ in range(max_words // 4 + 1):
            ws = self.link.get_some()
            if not ws:
                break
            for w in ws:
                if self._pend is not None:
                    kind = self._pend
                    self._pend = None
                    t = self.clock(w)
                    self.now = t
                    if kind == "start":
                        out.append(("start", t))
                    continue
                top = w >> 12
                if top == 0x8:
                    self._pend = "start"
                elif top == 0x9:
                    self._pend = "mark"
                elif top == 0xA:
                    out.append(("stop",))
                elif top == 0xC:
                    self.lost += w & 0xFFF
                    out.append(("lost", w & 0xFFF))
                else:
                    out.append(("byte", w & 0xFF, (w >> 8) & 1))
        return out

    @staticmethod
    def transactions(events):
        """Group events into transactions: dicts with the START time, the
        7-bit address, R/W, whether it was ACKed, and the data bytes with
        their ACK bits. A repeated START begins a new transaction."""
        txs = []
        cur = None
        for e in events:
            if e[0] == "start":
                cur = {"time": e[1], "addr": None, "read": None, "ack": None, "data": [], "stop": False}
                txs.append(cur)
            elif e[0] == "byte" and cur is not None:
                if cur["addr"] is None:
                    cur["addr"], cur["read"], cur["ack"] = e[1] >> 1, e[1] & 1, e[2] == 0
                else:
                    cur["data"].append((e[1], e[2] == 0))
            elif e[0] == "stop" and cur is not None:
                cur["stop"] = True
                cur = None
        return txs


class _NearClock:
    """Unwraps 16-bit timestamps that arrive nearly in order (each within
    32767 cycles of the previous one, either way)."""

    def __init__(self):
        self.prev = None
        self.full = 0

    def __call__(self, t):
        if self.prev is None:
            self.prev = t
            return 0
        d = ((t - self.prev + 32768) & 0xFFFF) - 32768
        self.full += d
        self.prev = t
        return self.full


LOST = 16
FLAG_NAMES = ("SKEW", "UNDER", "STUFFERR", "OVERRUN", "LOST")


class SpiMonitor:
    """Passive SPI monitor. Records MOSI and MISO bytes of each CS-framed
    transaction with the CS-fall time. The chip stores 16-bit words (one
    MOSI/MISO pair per 16 SCK edges); with keep=K only the first K bytes
    (rounded up to whole words) of each transaction are stored and the rest
    counted, which is what matters for "which commands and addresses did
    the MCU send" on long reads."""

    def __init__(self, link, mosi=0, miso=1, sck=2, cs=3, mode=0, keep=None, filter_cycles=0):
        self.link = link
        edge = 1 if mode in (0, 3) else 2  # MODE.XCLK: 1 rising, 2 falling
        link.load(IMAGES["spimon"], {
            "mosi": cs << 12 | sck << 8 | mosi << 4 | NOPIN,
            "miso": cs << 12 | sck << 8 | miso << 4 | NOPIN,
            "cs": NOPIN << 12 | NOPIN << 8 | cs << 4 | NOPIN,
            "edge": edge << 12 | (filter_cycles & 7) << 9,
            "keep": 0xFFFF if keep is None else (keep + 1) // 2,
        })
        self._keepw = None if keep is None else (keep + 1) // 2
        self.clock = _NearClock()
        self.lost = 0
        self.errors = []  # (time, names) of lane flags seen at CS rise
        self._need = 0    # words still owed to the current record
        self._rec = None
        self._pairs = []
        self._cur = None
        self.done = []
        self._marks = 0
        # the firmware's first time marker says both lanes are armed; bus
        # traffic before it would be missed on one lane only
        startup = []
        for _ in range(1000):
            startup += self.poll()
            if self._marks:
                break
        else:
            raise RuntimeError("SPI monitor firmware did not arm")
        self.done = startup + self.done

    def _word(self, w):
        kind = self._rec
        if kind == "pairs":
            self._pairs.append(w)
            self._need -= 1
            if self._need == 0:
                if self._cur is None:  # its start event was dropped
                    self._cur = {"time": None, "words": [], "lost": True}
                for k in range(0, len(self._pairs), 2):
                    self._cur["words"].append((self._pairs[k], self._pairs[k + 1]))
                self._pairs = []
                self._rec = None
            return
        self._rec = None
        if kind == "start":
            if self._drop and self._cur is not None:  # its end event was dropped
                cur = self._cur
                self.done.append(self._finish(cur, len(cur["words"]), 0, LOST))
            self._cur = {"time": self.clock(w), "words": []}
        elif kind == "mark":
            self.clock(w)
            self._marks += 1
        elif kind == "end":
            cur, bits = self._cur, self._endinfo & 15
            flags = (self._endinfo & 0xFF) >> 4
            if cur is None or cur.get("lost") or self._drop:
                flags |= LOST
                cur = cur or {"time": None, "words": []}
            self.done.append(self._finish(cur, w, bits, flags))
            self._cur = None

    def _finish(self, cur, count, bits, flags):
        mosi, miso = bytearray(), bytearray()
        words = cur["words"]
        for k, (a, b) in enumerate(words):
            if k == len(words) - 1 and bits:
                nb = bits // 8  # whole bytes in the cut-off word, right-aligned
                for j in range(nb):
                    mosi.append((a >> (bits - 8 * (j + 1))) & 0xFF)
                    miso.append((b >> (bits - 8 * (j + 1))) & 0xFF)
            else:
                mosi += bytes([a >> 8, a & 0xFF])
                miso += bytes([b >> 8, b & 0xFF])
        nbytes = 2 * (count - (1 if bits else 0)) + bits // 8
        if len(words) < count and (self._keepw is None or len(words) < self._keepw):
            flags |= LOST  # pairs dropped because the ring was full
        if flags:
            names = [n for b, n in enumerate(FLAG_NAMES) if flags >> b & 1]
            self.errors.append((cur["time"], names))
        return {"time": cur["time"], "mosi": mosi, "miso": miso, "count": nbytes,
                "partial_bits": bits % 8, "flags": flags}

    def poll(self, max_words=64):
        """Returns completed transactions: dicts with the CS-fall time (in
        cycles), mosi and miso bytes (the stored ones), the total byte
        count, bits of a trailing partial byte, and lane flags (bit 0
        SKEW: the MOSI and MISO lanes counted different numbers of SCK
        edges, so MISO is misaligned; bit 1 UNDER: SCK edges arrived with
        no receive queued, bits were lost; bit 3 OVERRUN; bit 4 LOST: the
        chip's buffers were full, so words or the transaction's start or
        end are missing and the record may span several transactions)."""
        for _ in range(max_words // 4 + 1):
            ws = self.link.get_some()
            if not ws:
                break
            for w in ws:
                if self._rec is not None:
                    self._word(w)
                    continue
                top = w >> 12
                if top == 0xD:
                    self._rec, self._need = "pairs", 2 * (w & 15)
                    if self._need == 0:
                        self._rec = None
                elif top == 0x8:
                    self._rec, self._drop = "start", w >> 8 & 1
                elif top == 0x9:
                    self._rec = "mark"
                elif top == 0xA:
                    self._rec, self._endinfo, self._drop = "end", w & 0xFF, w >> 8 & 1
                elif top == 0xC:
                    self.lost += w & 0xFF
        out, self.done = self.done, []
        return out
