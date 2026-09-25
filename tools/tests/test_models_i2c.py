"""Tests for tempo.models.i2c: I2cTarget/I2cBusMonitor driven by a small
pure-Python I2C master bit-banger, including deliberately wrong timing to
prove violations are caught.
"""

import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tempo.board import Board, Device
from tempo.models.i2c import I2cTarget, I2cBusMonitor


class I2cMaster(Device):
    """Bit-bangs I2C. Always releases SCL and polls for it to actually
    read high before proceeding (this is what a real master does, and is
    what makes clock stretching work), so `half` need not be tuned per
    test for stretching to be handled correctly.
    """

    def __init__(self, scl, sda, clock_hz=50e6, bit_hz=100e3):
        self.scl = scl
        self.sda = sda
        self.half = max(8, round(clock_hz / bit_hz / 2))
        self._prog = deque()
        self._micro = deque()
        self._scl_drive = None
        self._sda_drive = None
        self._ctr = 0

        self.acks = []       # ack bit (0=ack, 1=nack) per written byte
        self.read_bytes = []
        self._read_bits = []

    def start(self):
        self._prog.append(('S',))

    def rstart(self):
        self._prog.append(('RS',))

    def stop(self):
        self._prog.append(('P',))

    def write(self, byte):
        self._prog.append(('W', byte))

    def read(self, ack):
        self._prog.append(('R', ack))

    def idle(self):
        return not self._prog and not self._micro

    def _compile_next(self):
        op = self._prog.popleft()
        kind = op[0]
        half = self.half
        m = []
        if kind == 'S':
            m.append((None, None, 'fixed', half, None))
            m.append((None, 0, 'fixed', half, None))
            m.append((0, 0, 'fixed', half, None))
        elif kind == 'RS':
            m.append((0, None, 'fixed', half, None))
            m.append((None, None, 'wait_high', None, None))
            m.append((None, None, 'fixed', half, None))
            m.append((None, 0, 'fixed', half, None))
            m.append((0, 0, 'fixed', half, None))
        elif kind == 'P':
            m.append((0, 0, 'fixed', half, None))
            m.append((None, 0, 'wait_high', None, None))
            m.append((None, 0, 'fixed', half, None))
            m.append((None, None, 'fixed', half, None))
        elif kind == 'W':
            byte = op[1]
            bits = [(byte >> i) & 1 for i in range(7, -1, -1)]
            for b in bits:
                m.append((0, b, 'fixed', half, None))
                m.append((None, b, 'wait_high', None, None))
                m.append((None, b, 'fixed', half, None))
            m.append((0, None, 'fixed', half, None))
            m.append((None, None, 'wait_high', None, None))
            m.append((None, None, 'fixed', half, 'ack'))
            m.append((0, None, 'fixed', half, None))
        elif kind == 'R':
            ack = op[1]
            for _ in range(8):
                m.append((0, None, 'fixed', half, None))
                m.append((None, None, 'wait_high', None, None))
                m.append((None, None, 'fixed', half, 'bit'))
            ack_val = 0 if ack else None
            m.append((0, ack_val, 'fixed', half, None))
            m.append((None, ack_val, 'wait_high', None, None))
            m.append((None, ack_val, 'fixed', half, None))
            m.append((0, None, 'fixed', half, None))
        else:
            raise ValueError(kind)
        self._micro.extend(m)

    def step(self, cycle, nets):
        # A wait_high that resolves this cycle must fall through into the
        # following fixed step's ctr==0 handling (including any sample)
        # using this SAME nets snapshot, not next cycle's -- otherwise a
        # 'bit'/'ack' sample is taken one cycle late, against nets that
        # have already moved on.
        while True:
            if not self._micro:
                if not self._prog:
                    return {self.scl: None, self.sda: None}
                self._compile_next()

            scl_val, sda_val, mode, dur, sample = self._micro[0]
            if mode == 'wait_high':
                self._scl_drive = None
                self._sda_drive = sda_val
                if nets.get(self.scl, 1) == 1:
                    self._micro.popleft()
                    self._ctr = 0
                    continue
                return {self.scl: None, self.sda: self._sda_drive}

            # mode == 'fixed'
            if self._ctr == 0:
                self._scl_drive = scl_val
                self._sda_drive = sda_val
                if sample == 'ack':
                    self.acks.append(nets.get(self.sda, 1))
                elif sample == 'bit':
                    self._read_bits.append(nets.get(self.sda, 1))
                    if len(self._read_bits) == 8:
                        val = 0
                        for b in self._read_bits:
                            val = (val << 1) | b
                        self.read_bytes.append(val)
                        self._read_bits = []
            self._ctr += 1
            if self._ctr >= dur:
                self._micro.popleft()
                self._ctr = 0
            return {self.scl: self._scl_drive, self.sda: self._sda_drive}


def _run_until_idle(board, master, max_cycles=2_000_000, settle_cycles=10):
    # See the SPI test harness: the board delivers each device's drive to
    # others one cycle later, so give the target a few extra ticks after
    # the master itself goes idle to observe its final action (STOP).
    for _ in range(max_cycles):
        board.tick({})
        if master.idle():
            for _ in range(settle_cycles):
                board.tick({})
            return
    raise AssertionError('master did not finish within max_cycles')


def test_write_register_pointer_then_data():
    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    master = board.attach(I2cMaster('scl', 'sda', clock_hz=50e6, bit_hz=100e3))
    target = board.attach(I2cTarget('scl', 'sda', 0x50, regs=bytearray(64)))
    mon = board.attach(I2cBusMonitor('scl', 'sda'))

    master.start()
    master.write(0x50 << 1)       # address + write
    master.write(0x10)            # register pointer
    master.write(0xAB)
    master.write(0xCD)
    master.stop()
    _run_until_idle(board, master)

    assert master.acks == [0, 0, 0, 0]
    assert target.regs[0x10:0x12] == b'\xab\xcd'
    assert target.violations == []
    assert mon.violations == []


def test_read_after_address_without_repeated_start():
    # Direct read (address+R as the very first thing) exercises the same
    # target-driven-address-ack -> target-driven-data-bits path as a
    # repeated-start read, without a preceding write phase.
    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    master = board.attach(I2cMaster('scl', 'sda', clock_hz=50e6, bit_hz=100e3))
    regs = bytearray(64)
    regs[0] = 0x42
    regs[1] = 0x43
    target = board.attach(I2cTarget('scl', 'sda', 0x50, regs=regs))
    mon = board.attach(I2cBusMonitor('scl', 'sda'))

    master.start()
    master.write(0x50 << 1 | 1)   # address + read, pointer defaults to 0
    master.read(True)             # ack -> continue
    master.read(False)            # nack -> stop reading
    master.stop()
    _run_until_idle(board, master)

    assert master.acks == [0]     # only the address byte's ack
    assert master.read_bytes == [0x42, 0x43]
    assert target.violations == [], target.violations
    assert mon.violations == [], mon.violations


def test_write_pointer_then_repeated_start_read():
    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    master = board.attach(I2cMaster('scl', 'sda', clock_hz=50e6, bit_hz=100e3))
    regs = bytearray(range(0x80, 0x80 + 64))
    target = board.attach(I2cTarget('scl', 'sda', 0x50, regs=regs))
    mon = board.attach(I2cBusMonitor('scl', 'sda'))

    master.start()
    master.write(0x50 << 1)
    master.write(0x05)            # set pointer to 5
    master.rstart()
    master.write(0x50 << 1 | 1)
    master.read(True)
    master.read(True)
    master.read(False)
    master.stop()
    _run_until_idle(board, master)

    assert master.acks == [0, 0, 0]
    assert master.read_bytes == [regs[5], regs[6], regs[7]]
    assert target.violations == [], target.violations
    assert mon.violations == [], mon.violations

    starts = [e for e in mon.events if e.kind in ('start', 'repeated_start')]
    assert [e.kind for e in starts] == ['start', 'repeated_start']


def test_nack_on_wrong_address():
    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    master = board.attach(I2cMaster('scl', 'sda', clock_hz=50e6, bit_hz=100e3))
    target = board.attach(I2cTarget('scl', 'sda', 0x50, regs=bytearray(16)))

    master.start()
    master.write(0x23 << 1)
    master.stop()
    _run_until_idle(board, master)

    assert master.acks == [1]  # nacked: address didn't match
    assert target.violations == []


def test_clock_stretching():
    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    master = board.attach(I2cMaster('scl', 'sda', clock_hz=50e6, bit_hz=100e3))
    regs = bytearray(range(16))
    target = board.attach(I2cTarget('scl', 'sda', 0x50, regs=regs, stretch_cycles=300))
    mon = board.attach(I2cBusMonitor('scl', 'sda'))

    master.start()
    master.write(0x50 << 1)
    master.write(0x02)
    master.rstart()
    master.write(0x50 << 1 | 1)
    master.read(False)
    master.stop()
    _run_until_idle(board, master)

    assert master.acks == [0, 0, 0]
    assert master.read_bytes == [regs[2]]
    assert target.violations == [], target.violations
    assert mon.violations == [], mon.violations


def test_nack_after_stops_write():
    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    master = board.attach(I2cMaster('scl', 'sda', clock_hz=50e6, bit_hz=100e3))
    target = board.attach(I2cTarget('scl', 'sda', 0x50, regs=bytearray(16), nack_after=1))

    master.start()
    master.write(0x50 << 1)
    master.write(0x00)   # reg pointer: acked
    master.write(0xAA)   # 1st data byte: acked (nack_after=1)
    master.write(0xBB)   # 2nd data byte: nacked
    master.stop()
    _run_until_idle(board, master)

    assert master.acks == [0, 0, 0, 1]
    assert target.regs[0] == 0xAA
    assert target.regs[1] != 0xBB


class _RawDriver(Device):
    def __init__(self, frames):
        self.frames = frames
        self.idx = 0

    def step(self, cycle, nets):
        if self.idx < len(self.frames):
            f = self.frames[self.idx]
            self.idx += 1
            return f
        return self.frames[-1] if self.frames else {}


def _hold(scl, sda, cycles):
    return [{'scl': scl, 'sda': sda}] * cycles


def test_scl_low_timing_violation():
    # Fast mode min t_low is 1.3us; at 50MHz that's 65 cycles. Construct a
    # START followed by two SCL pulses with an illegally short low time
    # in between.
    frames = []
    frames += _hold(None, None, 20)      # idle
    frames += _hold(None, 0, 20)         # START: sda falls while scl high (released, pulled up)
    frames += _hold(0, 0, 20)            # scl low (normal)
    frames += _hold(1, 0, 20)            # scl high (bit, normal duration)
    frames += _hold(0, 0, 3)             # scl low for only 3 cycles: way under t_low minimum
    frames += _hold(1, 0, 20)            # scl high again
    frames += _hold(0, 0, 20)

    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    driver = board.attach(_RawDriver(frames))
    mon = board.attach(I2cBusMonitor('scl', 'sda', mode='fast', clock_hz=50e6))

    for _ in range(len(frames) + 5):
        board.tick({})

    low_violations = [v for v in mon.violations if v.kind == 'scl_low_short']
    assert low_violations, f'expected scl_low_short, got {mon.violations}'


def test_sda_glitch_detected():
    # SDA changes twice while SCL stays high in one stretch: the first
    # change is a legitimate (repeated) START, the second is a glitch.
    frames = []
    frames += _hold(None, None, 20)
    frames += _hold(None, 0, 5)     # first change while scl high: START
    frames += _hold(None, 1, 5)     # second change while scl STILL high: illegal
    frames += _hold(None, 1, 20)

    board = Board(clock_hz=50e6, pullups={'scl', 'sda'})
    driver = board.attach(_RawDriver(frames))
    mon = board.attach(I2cBusMonitor('scl', 'sda', clock_hz=50e6))

    for _ in range(len(frames) + 5):
        board.tick({})

    glitches = [v for v in mon.violations if v.kind == 'sda_glitch']
    assert glitches, f'expected sda_glitch, got {mon.violations}'
