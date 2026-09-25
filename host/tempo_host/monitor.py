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
