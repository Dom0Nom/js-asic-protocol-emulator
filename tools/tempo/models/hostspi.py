"""Host side of the Tempo host SPI port, as a board device.

Plays the role of the demo board RP2350: drives HCS_N (ui7), HSCK (ui5),
HMOSI (ui6) in SPI mode 0 and samples HMISO (uo3) on each rising SCK edge.
Transactions run one after another; each records the bytes read back.
"""

from collections import deque

from ..board import Device


class Transaction:
    def __init__(self, data, name=""):
        self.data = bytes(data)
        self.name = name
        self.rx = bytearray()
        self.done = False

    def words(self, skip):
        b = self.rx[skip:]
        return [(b[i] << 8) | b[i + 1] for i in range(0, len(b) - 1, 2)]


class HostDriver(Device):
    def __init__(self, half_period=6, cs="ui7", sck="ui5", mosi="ui6", miso="uo3", gap=4):
        self.half = half_period
        self.nets = (cs, sck, mosi, miso)
        self.gap = gap
        self.queue = deque()
        self.cur = None
        self.phase = 0
        self.timer = 0
        self.bit = 0
        self.levels = {cs: 1, sck: 0, mosi: 0}
        self.idle_timer = gap

    # ---- transaction builders
    def xfer(self, data, name=""):
        t = Transaction(data, name)
        self.queue.append(t)
        return t

    def write_imem(self, addr, words):
        data = [0x01, addr >> 8, addr & 0xFF]
        for w in words:
            data += [w >> 8, w & 0xFF]
        return self.xfer(data, "write_imem")

    def read_imem(self, addr, n):
        return self.xfer([0x02, addr >> 8, addr & 0xFF, 0] + [0] * (2 * n), "read_imem")  # words(4)

    def control(self, run_set=0, run_clear=0):
        return self.xfer([0x05, (run_clear << 4) | run_set], "control")

    def set_pc(self, thread, pc):
        return self.xfer([0x06, thread, pc >> 8, pc & 0xFF], "set_pc")

    def status(self):
        return self.xfer([0x07, 0, 0, 0, 0, 0], "status")  # dummy, then 4 bytes (rx[2:6])

    def mbox_write(self, words):
        data = [0x08]
        for w in words:
            data += [w >> 8, w & 0xFF]
        return self.xfer(data, "mbox_write")

    def mbox_read(self, n):
        return self.xfer([0x09, 0] + [0] * (2 * n), "mbox_read")  # dummy, then words (words(2))

    def step_thread(self, thread):
        return self.xfer([0x0A, thread], "step")

    def read_reg(self, thread, reg):
        return self.xfer([0x0B, thread, reg, 0, 0, 0], "read_reg")  # word in rx[4:6]

    def boot(self, prog, threads=None):
        """Load a program image and start the entry threads."""
        used = sorted(prog.used)
        if used:
            lo, hi = used[0], used[-1]
            self.write_imem(lo, prog.words[lo:hi + 1])
        mask = 0
        for t, pc in sorted(prog.entries.items()):
            if threads is None or t in threads:
                self.set_pc(t, pc)
                mask |= 1 << t
        return self.control(run_set=mask)

    @property
    def busy(self):
        return self.cur is not None or bool(self.queue)

    # ---- device
    def step(self, cycle, nets):
        cs, sck, mosi, miso = self.nets
        if self.cur is None:
            if self.idle_timer:
                self.idle_timer -= 1
            elif self.queue:
                self.cur = self.queue.popleft()
                self.bit = 0
                self.phase = 0
                self.timer = self.half
                self.levels[cs] = 0
                self.levels[mosi] = self.cur_bit()
            return dict(self.levels)
        self.timer -= 1
        if self.timer <= 0:
            self.timer = self.half
            if self.phase == 0:
                # rising edge: target samples MOSI, we sample MISO
                self.levels[sck] = 1
                if self.bit % 8 == 0:
                    self.cur.rx.append(0)
                self.cur.rx[-1] = (self.cur.rx[-1] << 1 | (nets.get(miso, 0) & 1)) & 0xFF
                self.phase = 1
            else:
                self.levels[sck] = 0
                self.bit += 1
                self.phase = 0
                if self.bit == 8 * len(self.cur.data):
                    self.levels[cs] = 1
                    self.cur.done = True
                    self.cur = None
                    self.idle_timer = self.gap * self.half
                else:
                    self.levels[mosi] = self.cur_bit()
        return dict(self.levels)

    def cur_bit(self):
        byte = self.cur.data[self.bit // 8]
        return (byte >> (7 - self.bit % 8)) & 1
