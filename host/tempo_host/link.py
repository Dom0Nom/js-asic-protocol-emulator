# SPDX-License-Identifier: Apache-2.0
"""Host side of the Tempo host SPI port.

Runs unchanged under CPython and MicroPython. A transport supplies
`xfer(data) -> bytes` (one CS-framed SPI transaction, full duplex),
`sleep_us(us)` and `ticks_us()`. See docs/architecture.md, Host port.
"""

CMD_WRITE_IMEM = 0x01
CMD_READ_IMEM = 0x02
CMD_CONTROL = 0x05
CMD_SET_PC = 0x06
CMD_STATUS = 0x07
CMD_MBOX_WRITE = 0x08
CMD_MBOX_READ = 0x09
CMD_STEP = 0x0A
CMD_READ_REG = 0x0B

MBOX_DEPTH = 4

# Lane sticky flags as reported in status bytes (lane STAT >> 3).
LANE_FLAG_NAMES = ("start", "stop", "LATE", "UNDER", "STUFFERR", "OVERRUN", "ARBLOST")
LANE_ERRORS = 0x7C  # LATE, UNDER, STUFFERR, OVERRUN, ARBLOST


class TempoError(Exception):
    pass


class Status:
    """Decoded host status (command 0x07)."""

    def __init__(self, raw):
        self.raw = bytes(raw)
        self.run = raw[0] & 15
        self.illegal = raw[0] >> 4
        self.t2h = raw[1] >> 4
        self.h2t = raw[1] & 15
        self.lane_flags = raw[2] & 0x7F
        self.pinconflict = raw[2] >> 7
        self.late = raw[3] & 15
        self.lanes = list(raw[4:8]) if len(raw) >= 8 else None

    def errors(self):
        """Names of every error flag that is set; empty when clean."""
        out = []
        for t in range(4):
            if (self.illegal >> t) & 1:
                out.append("T%d illegal instruction" % t)
            if (self.late >> t) & 1:
                out.append("T%d LATE" % t)
        if self.pinconflict:
            out.append("PINCONFLICT")
        if self.lanes is not None:
            for k, f in enumerate(self.lanes):
                for b, name in enumerate(LANE_FLAG_NAMES):
                    if (LANE_ERRORS >> b) & 1 and (f >> b) & 1:
                        out.append("lane %d %s" % (k, name))
        else:
            for b, name in enumerate(LANE_FLAG_NAMES):
                if (LANE_ERRORS >> b) & 1 and (self.lane_flags >> b) & 1:
                    out.append("lane %s" % name)
        return out

    def __repr__(self):
        return "Status(run=%x illegal=%x t2h=%d h2t=%d flags=%s)" % (
            self.run, self.illegal, self.t2h, self.h2t, ",".join(self.errors()) or "ok")


def _w16(words):
    b = bytearray()
    for w in words:
        b.append((w >> 8) & 0xFF)
        b.append(w & 0xFF)
    return b


def _words(b):
    return [(b[i] << 8) | b[i + 1] for i in range(0, len(b) - 1, 2)]


class Link:
    """Transaction-level access to one Tempo chip."""

    def __init__(self, transport):
        self.t = transport
        self.image = None
        self.errors_seen = []

    # ---- raw commands
    def write_imem(self, addr, words, chunk=64):
        for i in range(0, len(words), chunk):
            part = words[i:i + chunk]
            a = addr + i
            self.t.xfer(bytes([CMD_WRITE_IMEM, a >> 8, a & 0xFF]) + _w16(part))

    def read_imem(self, addr, n):
        rx = self.t.xfer(bytes([CMD_READ_IMEM, addr >> 8, addr & 0xFF, 0]) + bytes(2 * n))
        return _words(rx[4:])

    def control(self, run_set=0, run_clear=0):
        self.t.xfer(bytes([CMD_CONTROL, (run_clear << 4) | run_set]))

    def set_pc(self, thread, pc):
        self.t.xfer(bytes([CMD_SET_PC, thread, pc >> 8, pc & 0xFF]))

    def step(self, thread):
        self.t.xfer(bytes([CMD_STEP, thread]))

    def read_reg(self, thread, reg):
        """reg 0-7: r0-r7, 8: PC, 9: flags. Thread must be halted."""
        rx = self.t.xfer(bytes([CMD_READ_REG, thread, reg, 0, 0, 0]))
        return (rx[4] << 8) | rx[5]

    def status(self):
        rx = self.t.xfer(bytes([CMD_STATUS]) + bytes(9))
        return Status(rx[2:])

    # ---- mailboxes
    # Both mailbox commands answer, in the byte after the command, how many
    # words the port will take (write: free H2T space) or give (read: T2H
    # level), plus bit 7 = some error flag is set. The port never pushes or
    # pops more than that, so the host always knows exactly what happened.
    def _flag(self, b):
        if b & 0x80:
            self.errors_seen.append(self.status())

    def put(self, words, timeout_us=100000):
        """Send words to the threads (H2T), resending whatever did not fit."""
        words = list(words)
        t0 = self.t.ticks_us()
        while words:
            part = words[:MBOX_DEPTH]
            rx = self.t.xfer(bytes([CMD_MBOX_WRITE]) + _w16(part))
            self._flag(rx[1])
            took = min(rx[1] & 7, len(part))
            words = words[took:]
            if took:
                t0 = self.t.ticks_us()
            elif self.t.ticks_us() - t0 > timeout_us:
                raise TempoError("mailbox to chip stayed full for %d us: %s" % (timeout_us, self.status()))
            else:
                self.t.sleep_us(2)

    def get_some(self, max_words=MBOX_DEPTH):
        """Read up to max_words from T2H (fewer if fewer are waiting)."""
        n = min(max_words, MBOX_DEPTH)
        rx = self.t.xfer(bytes([CMD_MBOX_READ, 0]) + bytes(2 * n))
        self._flag(rx[1])
        level = rx[1] & 7
        return _words(rx[2:2 + 2 * min(level, n)])

    def get(self, n, timeout_us=100000):
        """Read exactly n words from the threads, waiting for them."""
        out = []
        t0 = self.t.ticks_us()
        while len(out) < n:
            got = self.get_some(n - len(out))
            if got:
                out += got
                t0 = self.t.ticks_us()
            elif self.t.ticks_us() - t0 > timeout_us:
                raise TempoError("timed out waiting for %d words from the chip, got %d: %s"
                                 % (n, len(out), self.status()))
            else:
                self.t.sleep_us(2)
        return out

    # ---- firmware
    def halt(self, mask=15):
        self.control(run_clear=mask)

    def load(self, image, params=None, start=True):
        """Load a firmware image (see images.py), patch its parameter block,
        set the entry PCs and start the entry threads."""
        threads = 0
        for t in image["entry"]:
            threads |= 1 << t
        self.halt(15)
        words = list(image["words"])
        base = image["base"]
        p = image["params"]
        for name, value in (params or {}).items():
            if name not in p:
                raise TempoError("image %s has no parameter %s" % (image["name"], name))
            words[p[name] - base] = value & 0xFFFF
        self.write_imem(base, words)
        self.param_values = {}
        for name, a in p.items():
            self.param_values[name] = words[a - base]
        # drain anything a previous image left in the thread-to-host mailbox
        while self.get_some():
            pass
        # sorted: MicroPython dicts do not keep insertion order, and the
        # transaction sequence should be the same on every port
        for t, pc in sorted(image["entry"].items()):
            self.set_pc(t, pc)
        self.image = image
        if start:
            self.control(run_set=threads)

    def patch(self, params):
        """Change parameters of the loaded image and restart its threads
        from their entry points, without reloading the code."""
        image = self.image
        threads = 0
        for t in image["entry"]:
            threads |= 1 << t
        self.halt(threads)
        for name, value in sorted(params.items()):
            a = image["params"][name]
            self.write_imem(a, [value & 0xFFFF])
            self.param_values[name] = value & 0xFFFF
        while self.get_some():
            pass
        for t, pc in sorted(image["entry"].items()):
            self.set_pc(t, pc)
        self.control(run_set=threads)

    def check(self, allow=()):
        """Raise if the chip reports any error flag not in allow. Lane flags
        stay set until firmware clears them (`stat`)."""
        st = self.status()
        self.errors_seen = []
        errs = [e for e in st.errors() if not any(a in e for a in allow)]
        if errs:
            raise TempoError("chip reports: " + ", ".join(errs))
        return st
