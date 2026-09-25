"""Tests for tempo.models.uart: UartTx feeding UartRx on a Board."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tempo.board import Board, Device
from tempo.models.uart import UartTx, UartRx, UartError


def _run(board, cycles):
    for _ in range(cycles):
        board.tick({})


def test_basic_roundtrip_no_parity():
    clock_hz = 1_000_000
    baud = 9600
    board = Board(clock_hz=clock_hz)
    tx = board.attach(UartTx('line', baud, clock_hz=clock_hz))
    rx = board.attach(UartRx('line', baud, clock_hz=clock_hz))

    data = [0x55, 0xA5, 0x00, 0xFF, 0x3C]
    tx.send(data)
    _run(board, round((10 * len(data) + 5) * clock_hz / baud))

    assert rx.bytes == data
    assert rx.errors == []


def test_even_parity_roundtrip():
    clock_hz = 1_000_000
    baud = 19200
    board = Board(clock_hz=clock_hz)
    tx = board.attach(UartTx('line', baud, parity='even', clock_hz=clock_hz))
    rx = board.attach(UartRx('line', baud, parity='even', clock_hz=clock_hz))

    data = [0x00, 0x01, 0xFF, 0x81]
    tx.send(data)
    _run(board, round((11 * len(data) + 5) * clock_hz / baud))

    assert rx.bytes == data
    assert rx.errors == []


def test_odd_parity_roundtrip():
    clock_hz = 1_000_000
    baud = 19200
    board = Board(clock_hz=clock_hz)
    tx = board.attach(UartTx('line', baud, parity='odd', clock_hz=clock_hz))
    rx = board.attach(UartRx('line', baud, parity='odd', clock_hz=clock_hz))

    data = [0x00, 0x01, 0xFF, 0x81, 0x55]
    tx.send(data)
    _run(board, round((11 * len(data) + 5) * clock_hz / baud))

    assert rx.bytes == data
    assert rx.errors == []


def test_gap_between_bytes():
    clock_hz = 1_000_000
    baud = 9600
    board = Board(clock_hz=clock_hz)
    tx = board.attach(UartTx('line', baud, clock_hz=clock_hz))
    rx = board.attach(UartRx('line', baud, clock_hz=clock_hz))

    data = [0x12, 0x34]
    tx.send(data, gap_bits=20)
    _run(board, round((10 * len(data) + 20 * len(data) + 10) * clock_hz / baud))

    assert rx.bytes == data


class _RawUartFrame(Device):
    """Drives one hand-timed UART frame for deterministic negative tests."""

    def __init__(self, net, bit_levels, cycles_per_bit):
        self.net = net
        self.bit_levels = bit_levels
        self.cpb = cycles_per_bit
        self._cycle_in_bit = 0
        self._idx = 0

    def step(self, cycle, nets):
        if self._idx >= len(self.bit_levels):
            return {self.net: 1}
        level = self.bit_levels[self._idx]
        self._cycle_in_bit += 1
        if self._cycle_in_bit >= self.cpb:
            self._cycle_in_bit = 0
            self._idx += 1
        return {self.net: level}


def test_framing_error_on_bad_stop_bit():
    clock_hz = 1_000_000
    baud = 9600
    cpb = clock_hz / baud
    board = Board(clock_hz=clock_hz)
    # start(0), 8 data bits (0x55), stop bit driven LOW instead of high
    bits = [0] + [(0x55 >> i) & 1 for i in range(8)] + [0]
    board.attach(_RawUartFrame('line', bits, cpb))
    rx = board.attach(UartRx('line', baud, clock_hz=clock_hz))

    _run(board, round(cpb * (len(bits) + 2)))

    assert len(rx.bytes) == 1
    assert rx.bytes[0] == 0x55
    assert any(e.kind == 'framing' for e in rx.errors), rx.errors


def test_parity_error_detected():
    clock_hz = 1_000_000
    baud = 9600
    cpb = clock_hz / baud
    board = Board(clock_hz=clock_hz)
    data_bits = [(0x03 >> i) & 1 for i in range(8)]  # two 1-bits -> even parity should be 0
    bad_parity = 1  # deliberately wrong
    bits = [0] + data_bits + [bad_parity, 1]
    board.attach(_RawUartFrame('line', bits, cpb))
    rx = board.attach(UartRx('line', baud, parity='even', clock_hz=clock_hz))

    _run(board, round(cpb * (len(bits) + 2)))

    assert len(rx.bytes) == 1
    assert any(e.kind == 'parity' for e in rx.errors), rx.errors


def test_baud_mismatch_measured_period():
    clock_hz = 1_000_000
    baud = 9600
    board = Board(clock_hz=clock_hz)
    tx = board.attach(UartTx('line', baud, clock_hz=clock_hz, baud_error=0.10))
    rx = board.attach(UartRx('line', baud, clock_hz=clock_hz))

    tx.send([0x55])  # alternating bits -> plenty of edges to measure from
    _run(board, round(15 * clock_hz / baud))

    assert len(rx.bit_periods) == 1
    measured = rx.bit_periods[0]
    assert measured is not None
    nominal = clock_hz / baud
    assert abs(measured - nominal) / nominal > 0.05, (measured, nominal)


def test_severe_baud_mismatch_causes_error():
    clock_hz = 1_000_000
    baud = 9600
    board = Board(clock_hz=clock_hz)
    tx = board.attach(UartTx('line', baud, clock_hz=clock_hz, baud_error=0.30))
    rx = board.attach(UartRx('line', baud, clock_hz=clock_hz))

    tx.send([0x55, 0x55, 0x55])
    _run(board, round(40 * clock_hz / baud))

    assert rx.errors, 'expected a decode error from a 30% baud mismatch'
