# SPDX-License-Identifier: Apache-2.0
"""UART console bridge and autobaud.

Runs the "uart" firmware image (fw/uart.s): lane 0 transmits, lane 1
receives into a 512-character ring in the chip's SRAM, a third thread
forwards the ring to the host. Autobaud uses the "autobaud" image
(fw/autobaud.s): edge timestamps from capture mode, analysed here.
"""

from .images import CLOCK_HZ, IMAGES
from .link import TempoError

NOPIN = 15
E_D, E_DEN = 1, 16
SYM_HI = (1 << 15) | (1 << 14)
P_I, PP_I = 2, 32

STANDARD_BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 31250, 38400, 57600,
                  74880, 76800, 115200, 128000, 153600, 230400, 250000, 256000, 460800, 500000,
                  576000, 921600, 1000000, 1500000, 2000000, 3000000)

FE, PE, BREAK, LOST = 1 << 9, 1 << 10, 1 << 11, 1 << 15


def _tx_insn(lane, rs, n):
    # tx L, rs, n (docs/architecture.md encoding table, op 0xE)
    return 0xE << 12 | lane << 10 | (n - 1) << 6 | rs << 3


def _rxn_insn(lane, n):
    # rxn L, n (op 0xF, sub 1)
    return 0xF << 12 | lane << 10 | (n - 1) << 6 | 1


def bit_time(baud, clock_hz=CLOCK_HZ):
    """(prescaler, integer length, 1/256 fraction) for one bit."""
    if baud <= 0 or clock_hz <= 0:
        raise ValueError("baud and clock must be positive")
    cyc = clock_hz / baud
    for pre in range(8):  # MODE has a 3-bit prescaler
        units = int(round(cyc * 256 / (1 << pre)))
        # SAMP is 13 bits, so the midpoint must fit even though LEN is
        # 16 bits. Round the entire fixed-point value to retain a carry.
        if 256 <= units < 16384 * 256:
            return pre, units >> 8, units & 0xFF
    raise ValueError("baud cannot be represented at this chip clock")


class Uart:
    def __init__(self, link, tx=0, rx=1, baud=115200, bits=8, parity=None, stop=1,
                 invert_tx=False, invert_rx=False):
        """tx, rx: Tempo pin indices (0-7 uio, 8-14 uo/ui). parity: None,
        'even' or 'odd'. stop: 1 or 2."""
        if not 5 <= bits <= 9 or stop not in (1, 2):
            raise ValueError("5-9 data bits, 1 or 2 stop bits")
        self.link = link
        self.bits = bits
        self.baud = baud
        p = 0 if parity is None else (1 if parity == "even" else 2)
        np = 1 if p else 0
        pre, whole, frac = bit_time(baud, link.clock_hz)
        n_tx = 1 + bits + np + stop
        n_rx = 1 + bits + np + 1  # receive ends mid first stop bit
        stoppos = 1 + bits + np
        im = IMAGES["uart"]
        params = {
            "txpins": NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | tx,
            "rxpins": NOPIN << 12 | NOPIN << 8 | rx << 4 | NOPIN,
            "len": whole, "frac": frac, "samp": whole // 2, "mode": pre,
            "sym0": SYM_HI if invert_tx else 0,
            "sym1": 0 if invert_tx else SYM_HI,
            "idle": E_DEN | (0 if invert_tx else E_D),
            # start edge: falling (1 -> 0), or rising when the line is inverted
            "pat": (PP_I | P_I) << 8 | (P_I if invert_rx else PP_I),
            "xor": (1 << n_rx) - 1 if invert_rx else 0,
            "dmask": (1 << bits) - 1,
            "parity": p, "parpos": 1 + bits, "stoppos": stoppos,
            "stopmask": ((1 << stop) - 1) << stoppos,
            "txop": _tx_insn(0, 1, n_tx),
            "rxop": _rxn_insn(1, n_rx),
        }
        link.load(im, params)
        if link.get(1)[0] != 0:
            raise TempoError("UART receive startup failed")
        self.errors = 0

    @property
    def actual_baud(self):
        pre, whole, frac = bit_time(self.baud, self.link.clock_hz)
        return self.link.clock_hz / ((whole + frac / 256) * (1 << pre))

    def write(self, data):
        data = bytes(data) if self.bits <= 8 else data
        if self.bits == 9:
            for c in data:
                self.link.put([1, c])
            return
        for i in range(0, len(data), 64):
            part = data[i:i + 64]
            words = [len(part)]
            for j in range(0, len(part), 2):
                words.append(part[j] | ((part[j + 1] if j + 1 < len(part) else 0) << 8))
            self.link.put(words)

    def read_raw(self, max_chars=64):
        """Received characters as words with their error bits (FE, PE, BREAK,
        LOST), without waiting."""
        return self.link.get_some(min(max_chars, 4))

    def flush(self, quiet_us=2000):
        """Discard received characters until the line has been quiet for
        quiet_us (e.g. the rest of a banner after autobaud)."""
        t = self.link.t
        last = t.ticks_us()
        while t.ticks_us() - last < quiet_us:
            if self.link.get_some():
                last = t.ticks_us()
            else:
                t.sleep_us(100)

    def read(self, n, timeout_us=200000, strict=True):
        """n received characters; raises on framing/parity errors or ring
        overflow when strict."""
        words = self.link.get(n, timeout_us)
        bad = [w for w in words if w & (FE | PE | LOST)]
        if bad and strict:
            raise TempoError("UART receive errors: %s" % ", ".join(describe(w) for w in bad[:4]))
        return bytes(w & 0xFF for w in words) if self.bits <= 8 else [w & 0x1FF for w in words]


def describe(w):
    f = []
    if w & FE:
        f.append("framing")
    if w & PE:
        f.append("parity")
    if w & BREAK:
        f.append("break")
    if w & LOST:
        f.append("overflow before")
    return "0x%02x(%s)" % (w & 0x1FF, "+".join(f))


def snap_baud(measured, tolerance=0.02):
    """Nearest standard rate within tolerance, else the measured rate."""
    best = min(STANDARD_BAUDS, key=lambda b: abs(b - measured) / b)
    if abs(best - measured) / best <= tolerance:
        return best
    return int(round(measured))


def estimate_bit(widths):
    """Bit time from pulse widths (cycles): the shortest cluster sets the
    unit, then every width is assigned a whole number of bits and the unit
    is refined by least squares over all of them."""
    widths = sorted(w for w in widths if w > 0)
    if len(widths) < 4:
        raise TempoError("too few edges to estimate a baud rate")
    unit = widths[0]
    # ignore a lone glitch: the shortest width should repeat within 25%
    for w in widths:
        if sum(1 for x in widths if abs(x - w) <= w / 4) >= 2:
            unit = w
            break
    num = den = 0
    for w in widths:
        k = int(w / unit + 0.5)
        if 1 <= k <= 10 and abs(w - k * unit) <= unit / 3:
            num += w * k
            den += k * k
    return num / den


class Autobaud:
    """Measure an unknown UART's bit time from its edges."""

    def __init__(self, link, rx=1, n=48, filter_cycles=0):
        self.link = link
        self.n = n
        link.load(IMAGES["autobaud"], {"pins": NOPIN << 12 | NOPIN << 8 | rx << 4 | NOPIN,
                                       "mode": 1 << 7 | (filter_cycles & 7) << 9, "n": n})

    def widths(self, timeout_us=2000000):
        """Pulse widths in cycles. Gaps longer than the thread's 30000-cycle
        watch window are skipped, so idle time never counts as a pulse."""
        w = self.link.get(self.n + 1, timeout_us)
        if w[-1] & (1 << 8):
            raise TempoError("edge FIFO overran: the line is too fast to autobaud")
        return w[:-1]

    def measure(self):
        w = self.widths()
        bit = estimate_bit(w)
        return self.link.clock_hz / bit, w


def autobaud(link, rx=1, tx=0, confirm=3, **frame):
    """Measure, snap to a standard rate, then confirm by receiving `confirm`
    framed characters without errors at that rate. Returns (baud, Uart)."""
    measured, _ = Autobaud(link, rx).measure()
    baud = snap_baud(measured)
    u = Uart(link, tx=tx, rx=rx, baud=baud, **frame)
    u.read(confirm)
    return baud, u
