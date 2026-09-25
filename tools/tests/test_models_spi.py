"""Tests for tempo.models.spi: SpiSlave driven by a small pure-Python
bit-banged SPI master, across all 4 modes, plus SpiFlashLike.
"""

import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tempo.board import Board, Device
from tempo.models.spi import SpiSlave, SpiFlashLike


class SpiMaster(Device):
    """Bit-bangs SPI as a master. Schedule is built in units of
    half_period board cycles per clock half-cycle, generous enough that
    the board's one-cycle-per-device propagation delay never causes a
    stale read: an edge driven at cycle N is only visible to other
    devices from cycle N+1, and a response to that edge (e.g. slave
    updating MISO) is only visible back to us from N+2, so 'sample'
    actions are offset a few cycles after the edge that triggers them.
    """

    def __init__(self, sck, mosi, miso, cs, mode=0, msb_first=True, half_period=20, sample_delay=6, cs_gap=10):
        self.sck = sck
        self.mosi = mosi
        self.miso = miso
        self.cs = cs
        self.msb_first = msb_first
        self.half_period = half_period
        self.sample_delay = sample_delay
        self.cs_gap = cs_gap
        self.cpol = 0 if mode in (0, 1) else 1
        self.cpha = 0 if mode in (0, 2) else 1

        self.received = []
        self._jobs = deque()
        self._active = False
        self._sck = self.cpol
        self._mosi = 0
        self._cs = 1
        self._events = {}
        self._offsets = []
        self._eidx = 0
        self._end_off = 0
        self._start_cycle = 0
        self._rx_bits = []

    def send(self, data_bytes):
        self._jobs.append(bytes(data_bytes))

    def idle(self):
        return not self._active and not self._jobs

    def _bits(self, byte):
        bits = [(byte >> i) & 1 for i in range(8)]
        if self.msb_first:
            bits.reverse()
        return bits

    def _from_bits(self, bits):
        order = reversed(bits) if self.msb_first else bits
        value = 0
        for i, b in enumerate(order):
            value |= (b << i)
        return value

    def _build_schedule(self, data_bytes):
        all_bits = []
        for byte in data_bytes:
            all_bits.extend(self._bits(byte))
        n = len(all_bits)
        events = {}

        def add(off, act, val=None):
            events.setdefault(off, []).append((act, val))

        add(0, 'mosi', all_bits[0] if (self.cpha == 0 and n) else 0)

        for i in range(n):
            if self.cpha == 0:
                sample_edge_off = self.half_period * (2 * i + 1)
                add(sample_edge_off, 'sck', 1 - self.cpol)
                add(sample_edge_off + self.sample_delay, 'sample')
                if i + 1 < n:
                    shift_off = self.half_period * (2 * i + 2)
                    add(shift_off, 'sck', self.cpol)
                    add(shift_off, 'mosi', all_bits[i + 1])
                else:
                    add(self.half_period * (2 * i + 2), 'sck', self.cpol)
            else:
                shift_off = self.half_period * (2 * i + 1)
                add(shift_off, 'sck', 1 - self.cpol)
                add(shift_off, 'mosi', all_bits[i])
                sample_edge_off = self.half_period * (2 * i + 2)
                add(sample_edge_off, 'sck', self.cpol)
                add(sample_edge_off + self.sample_delay, 'sample')

        end_off = self.half_period * (2 * n) + self.cs_gap
        return events, end_off

    def step(self, cycle, nets):
        if not self._active:
            if self._jobs:
                data = self._jobs.popleft()
                self._events, self._end_off = self._build_schedule(data)
                self._offsets = sorted(self._events)
                self._eidx = 0
                self._start_cycle = cycle
                self._active = True
                self._cs = 0
                self._sck = self.cpol
                self._rx_bits = []
                return {self.cs: 0, self.sck: self.cpol, self.mosi: self._mosi}
            return {self.cs: 1, self.sck: self.cpol, self.mosi: 0}

        off = cycle - self._start_cycle
        while self._eidx < len(self._offsets) and self._offsets[self._eidx] <= off:
            for act, val in self._events[self._offsets[self._eidx]]:
                if act == 'sck':
                    self._sck = val
                elif act == 'mosi':
                    self._mosi = val
                elif act == 'sample':
                    bit = nets.get(self.miso, 1)
                    self._rx_bits.append(bit)
                    if len(self._rx_bits) == 8:
                        self.received.append(self._from_bits(self._rx_bits))
                        self._rx_bits = []
            self._eidx += 1

        if off >= self._end_off:
            self._active = False
            self._cs = 1
            return {self.cs: 1, self.sck: self.cpol, self.mosi: 0}

        return {self.cs: self._cs, self.sck: self._sck, self.mosi: self._mosi}


def _run_until_idle(board, master, max_cycles=200000, settle_cycles=4):
    # The board delivers each device's drive to others one cycle later,
    # so other devices need a few extra ticks after the master itself
    # goes idle to observe its final action (e.g. CS deassert).
    for _ in range(max_cycles):
        board.tick({})
        if master.idle():
            for _ in range(settle_cycles):
                board.tick({})
            return
    raise AssertionError('master did not finish within max_cycles')


def test_spi_mode0_transfer():
    board = Board(clock_hz=1e6)
    master = board.attach(SpiMaster('sck', 'mosi', 'miso', 'cs', mode=0))
    slave = board.attach(SpiSlave('sck', 'mosi', 'miso', 'cs', mode=0, response=bytes([0xAA, 0xBB, 0xCC])))

    master.send([0x11, 0x22, 0x33])
    _run_until_idle(board, master)

    assert master.received == [0xAA, 0xBB, 0xCC]
    assert slave.transactions == [(bytes([0x11, 0x22, 0x33]), bytes([0xAA, 0xBB, 0xCC]))]
    assert slave.violations == []


def test_spi_all_modes_transfer():
    for mode in (0, 1, 2, 3):
        board = Board(clock_hz=1e6)
        master = board.attach(SpiMaster('sck', 'mosi', 'miso', 'cs', mode=mode))
        slave = board.attach(SpiSlave('sck', 'mosi', 'miso', 'cs', mode=mode, response=bytes([0x5A, 0xA5])))

        master.send([0xF0, 0x0F])
        _run_until_idle(board, master)

        assert master.received == [0x5A, 0xA5], f'mode {mode}'
        assert slave.transactions == [(bytes([0xF0, 0x0F]), bytes([0x5A, 0xA5]))], f'mode {mode}'
        assert slave.violations == [], f'mode {mode}: {slave.violations}'


def test_spi_lsb_first():
    board = Board(clock_hz=1e6)
    master = board.attach(SpiMaster('sck', 'mosi', 'miso', 'cs', mode=0, msb_first=False))
    slave = board.attach(SpiSlave('sck', 'mosi', 'miso', 'cs', mode=0, msb_first=False, response=bytes([0x81])))

    master.send([0x81])
    _run_until_idle(board, master)

    assert master.received == [0x81]
    assert slave.transactions == [(bytes([0x81]), bytes([0x81]))]


def test_spi_response_callable():
    board = Board(clock_hz=1e6)
    master = board.attach(SpiMaster('sck', 'mosi', 'miso', 'cs', mode=0))

    def echo_plus_one(idx, mosi_so_far):
        if not mosi_so_far:
            return 0
        return (mosi_so_far[-1] + 1) & 0xFF

    slave = board.attach(SpiSlave('sck', 'mosi', 'miso', 'cs', mode=0, response=echo_plus_one))

    master.send([0x10, 0x20, 0x30])
    _run_until_idle(board, master)

    # first response byte is generated before any mosi byte is known -> 0
    assert master.received == [0x00, 0x11, 0x21]


class _RawDriver(Device):
    """Drives an explicit, hand-timed sequence of {net: level} dicts, one
    per cycle, for constructing deliberately malformed SPI waveforms."""

    def __init__(self, frames):
        self.frames = frames
        self.idx = 0

    def step(self, cycle, nets):
        if self.idx < len(self.frames):
            frame = self.frames[self.idx]
            self.idx += 1
            return frame
        return self.frames[-1] if self.frames else {}

    def done(self):
        return self.idx >= len(self.frames)


def test_spi_setup_violation_detected():
    # Mode 0: sample on rising SCK. Change MOSI just 1 cycle before the
    # rising edge (min_setup_cycles default is 1, so this must be flagged
    # by a slave configured to require more).
    frames = []
    frames.append({'cs': 0, 'sck': 0, 'mosi': 1})
    for _ in range(9):
        frames.append({'cs': 0, 'sck': 0, 'mosi': 1})
    # flip mosi just before the rising edge (bad setup), then rising edge
    frames.append({'cs': 0, 'sck': 0, 'mosi': 0})
    frames.append({'cs': 0, 'sck': 1, 'mosi': 0})
    for _ in range(10):
        frames.append({'cs': 0, 'sck': 1, 'mosi': 0})
    frames.append({'cs': 0, 'sck': 0, 'mosi': 0})
    for _ in range(5):
        frames.append({'cs': 0, 'sck': 0, 'mosi': 0})
    frames.append({'cs': 1, 'sck': 0, 'mosi': 0})
    frames.append({'cs': 1, 'sck': 0, 'mosi': 0})

    board = Board(clock_hz=1e6)
    driver = board.attach(_RawDriver(frames))
    slave = board.attach(SpiSlave('sck', 'mosi', 'miso', 'cs', mode=0, min_setup_cycles=5, min_hold_cycles=1))

    for _ in range(len(frames) + 5):
        board.tick({})

    setup_violations = [v for v in slave.violations if v.kind == 'setup']
    assert setup_violations, f'expected a setup violation, got {slave.violations}'


def test_spi_cs_framing_violation():
    # CS asserted, exactly 4 clock pulses (not a multiple of 8), then
    # deasserted: must be flagged.
    board = Board(clock_hz=1e6)
    master = board.attach(SpiMaster('sck', 'mosi', 'miso', 'cs', mode=0))
    slave = board.attach(SpiSlave('sck', 'mosi', 'miso', 'cs', mode=0, response=b'\x00'))

    events, _ = master._build_schedule(bytes([0xF0]))
    truncated = {off: acts for off, acts in events.items() if off <= master.half_period * 8}
    master._events = truncated
    master._offsets = sorted(truncated)
    master._eidx = 0
    master._end_off = master.half_period * 8 + master.cs_gap
    master._active = True
    master._start_cycle = 0
    master._cs = 0

    board.devices.remove(master)
    board.devices.insert(0, master)

    for _ in range(400):
        board.tick({})
        if not master._active:
            for _ in range(4):
                board.tick({})
            break

    framing_violations = [v for v in slave.violations if v.kind == 'cs_framing']
    assert framing_violations, f'expected a cs_framing violation, got {slave.violations}'


def test_spi_flash_like():
    board = Board(clock_hz=1e6)
    master = board.attach(SpiMaster('sck', 'mosi', 'miso', 'cs', mode=0))
    mem = bytearray(range(256))
    flash = board.attach(SpiFlashLike('sck', 'mosi', 'miso', 'cs', mem=mem, jedec_id=(0xAA, 0x55, 0x01)))

    # JEDEC ID read
    master.send([0x9F, 0x00, 0x00, 0x00])
    _run_until_idle(board, master)
    assert master.received[1:4] == [0xAA, 0x55, 0x01]

    # read 4 bytes starting at address 0x000010
    master.received.clear()
    master.send([0x03, 0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00])
    _run_until_idle(board, master)
    assert master.received[4:8] == [mem[0x10], mem[0x11], mem[0x12], mem[0x13]]

    # page program at 0x000020, then read it back
    master.send([0x02, 0x00, 0x00, 0x20, 0xDE, 0xAD, 0xBE, 0xEF])
    _run_until_idle(board, master)
    assert bytes(mem[0x20:0x24]) == bytes([0xDE, 0xAD, 0xBE, 0xEF])

    master.received.clear()
    master.send([0x03, 0x00, 0x00, 0x20, 0x00, 0x00, 0x00, 0x00])
    _run_until_idle(board, master)
    assert master.received[4:8] == [0xDE, 0xAD, 0xBE, 0xEF]
