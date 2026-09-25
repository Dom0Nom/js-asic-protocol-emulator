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


class SpiMonitor:
    """Passive SPI monitor. Records MOSI and MISO bytes of each CS-framed
    transaction with the CS-fall time; with keep=K only the first K bytes of
    each transaction are recorded (the rest are counted), which is what
    matters for "which commands and addresses did the MCU send", at a
    higher SCK."""

    def __init__(self, link, mosi=0, miso=1, sck=2, cs=3, mode=0, keep=0xFFFF, filter_cycles=0):
        self.link = link
        edge = 1 if mode in (0, 3) else 2  # MODE.XCLK: 1 rising, 2 falling
        link.load(IMAGES["spimon"], {
            "mosi": cs << 12 | sck << 8 | mosi << 4 | NOPIN,
            "miso": cs << 12 | sck << 8 | miso << 4 | NOPIN,
            "cs": NOPIN << 12 | NOPIN << 8 | cs << 4 | NOPIN,
            "edge": edge << 12 | (filter_cycles & 7) << 9,
            "keep": keep,
        })
        self.clock = _Clock()
        self.lost = 0
        self.overruns = 0  # the chip reported an edge FIFO overrun: bytes were missed
        self._pend = None
        self._cur = None
        self.done = []

    def poll(self, max_words=64):
        """Returns completed transactions: dicts with the CS-fall time,
        mosi and miso bytes (recorded ones), the total byte count and any
        bits of a cut-off byte."""
        for _ in range(max_words // 4 + 1):
            ws = self.link.get_some()
            if not ws:
                break
            for w in ws:
                if self._pend is not None:
                    kind = self._pend
                    self._pend = None
                    if kind == "count":
                        if self._cur is not None:
                            self._cur["count"] = w
                            self.done.append(self._cur)
                        self._cur = None
                        continue
                    t = self.clock(w)
                    if kind == "start":
                        self._cur = {"time": t, "mosi": bytearray(), "miso": bytearray(),
                                     "count": None, "partial_bits": 0}
                    continue
                top = w >> 12
                if top == 0x8:
                    self._pend = "start"
                elif top == 0x9:
                    self._pend = "mark"
                elif top == 0xA:
                    if self._cur is None:
                        self._cur = {"time": None, "mosi": bytearray(), "miso": bytearray(),
                                     "count": None, "partial_bits": 0}
                    self._cur["partial_bits"] = w & 31
                    self._pend = "count"
                elif top == 0xC:
                    self.lost += w & 0xFFF
                elif self._cur is not None:
                    (self._cur["miso"] if w >> 8 else self._cur["mosi"]).append(w & 0xFF)
        for st in self.link.errors_seen:
            for e in st.errors():
                if "OVERRUN" in e:
                    self.overruns += 1
        self.link.errors_seen = []
        out, self.done = self.done, []
        return out
