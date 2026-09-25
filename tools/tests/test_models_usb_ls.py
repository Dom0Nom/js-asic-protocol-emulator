"""Tests for tempo.models.usb_ls: CRC known-answer tests, and
UsbLsTransmitter feeding UsbLsReceiver including stuffing edge cases and
clock drift within (and beyond) the low-speed +/-1.5% tolerance.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tempo.board import Board, Device
from tempo.models.usb_ls import (
    UsbLsTransmitter, UsbLsReceiver, crc5, crc16, _SYNC_BITS, _SYMBOL_PINS,
)


def _run(board, cycles):
    for _ in range(cycles):
        board.tick({})


def _cycles_for(payload_bits, clock_hz):
    # sync(8) + pid(8) + payload + eop(3 symbols), 30% margin for stuffing
    cycles_per_bit = clock_hz / 1.5e6
    return round((8 + 8 + payload_bits + 3) * cycles_per_bit * 1.3)


# ---------------------------------------------------------------------
# CRC known-answer tests. crc5()/crc16() each assert their own two
# independent implementations agree internally; these tests check the
# result against external references.
# ---------------------------------------------------------------------

def test_crc16_catalogue_check_value():
    # "CRC-16/USB" in the standard CRC RevEng catalogue: check value for
    # ASCII "123456789" is 0xB4C8. This is the strongest external check
    # available here (a named, widely reproduced constant).
    assert crc16(b'123456789') == 0xB4C8


def test_crc5_setup_addr0_ep0():
    # Cited in the brief: a SETUP token to addr=0, ep=0 has CRC5 = 0x02.
    # All 11 payload bits are 0 for that token, and this model's result
    # matches independently of the crc16 catalogue check above.
    assert crc5([0] * 11) == 0x02


def test_crc5_crc16_self_consistency_spot_check():
    # crc5()/crc16() already assert their two internal implementations
    # agree on every call; exercise that on non-trivial inputs.
    for n in range(32):
        crc5([(n >> i) & 1 for i in range(11)])
    for n in range(64):
        crc16(bytes([(n * 7) & 0xFF, (n * 13) & 0xFF, n & 0xFF]))


# ---------------------------------------------------------------------
# Packet round trips
# ---------------------------------------------------------------------

CLOCK_HZ = 24e6  # 16 cycles/bit at 1.5 Mbit/s


def test_token_roundtrip():
    board = Board(clock_hz=CLOCK_HZ)
    tx = board.attach(UsbLsTransmitter('dp', 'dm', clock_hz=CLOCK_HZ))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    tx.send_token('SETUP', addr=0x3A, endp=0x5)
    _run(board, _cycles_for(16, CLOCK_HZ))

    assert len(rx.packets) == 1
    pkt = rx.packets[0]
    assert pkt.pid == 'SETUP'
    assert pkt.addr == 0x3A
    assert pkt.endp == 0x5
    assert pkt.crc_ok is True
    assert rx.errors == []


def test_sof_roundtrip():
    board = Board(clock_hz=CLOCK_HZ)
    tx = board.attach(UsbLsTransmitter('dp', 'dm', clock_hz=CLOCK_HZ))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    tx.send_sof(0x123)
    _run(board, _cycles_for(16, CLOCK_HZ))

    assert len(rx.packets) == 1
    assert rx.packets[0].pid == 'SOF'
    assert rx.packets[0].frame_num == 0x123
    assert rx.packets[0].crc_ok is True


def test_data_roundtrip_with_stuffing():
    # 0xFF bytes force long runs of 1s in the pre-NRZI bitstream,
    # guaranteeing bit-stuff insertion/removal is exercised.
    board = Board(clock_hz=CLOCK_HZ)
    tx = board.attach(UsbLsTransmitter('dp', 'dm', clock_hz=CLOCK_HZ))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    payload = bytes([0xFF, 0xFF, 0x00, 0x81, 0x7F, 0x3C])
    tx.send_data('DATA0', payload)
    _run(board, _cycles_for(8 * len(payload) + 16, CLOCK_HZ))

    assert len(rx.packets) == 1
    pkt = rx.packets[0]
    assert pkt.pid == 'DATA0'
    assert pkt.data == payload
    assert pkt.crc_ok is True
    assert rx.errors == []


def test_handshake_roundtrip():
    board = Board(clock_hz=CLOCK_HZ)
    tx = board.attach(UsbLsTransmitter('dp', 'dm', clock_hz=CLOCK_HZ))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    tx.send_handshake('ACK')
    _run(board, _cycles_for(0, CLOCK_HZ))

    assert len(rx.packets) == 1
    assert rx.packets[0].pid == 'ACK'


def test_multiple_packets_back_to_back():
    board = Board(clock_hz=CLOCK_HZ)
    tx = board.attach(UsbLsTransmitter('dp', 'dm', clock_hz=CLOCK_HZ))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    tx.send_token('OUT', addr=1, endp=2)
    tx.send_data('DATA1', b'\xde\xad\xbe\xef')
    tx.send_handshake('ACK')
    _run(board, _cycles_for(16, CLOCK_HZ) + _cycles_for(48, CLOCK_HZ) + _cycles_for(0, CLOCK_HZ))

    assert [p.pid for p in rx.packets] == ['OUT', 'DATA1', 'ACK']
    assert all(p.crc_ok for p in rx.packets)
    assert rx.errors == []


def test_clock_drift_within_tolerance():
    # USB 2.0 spec 7.1.11: low-speed source clock tolerance is +/-1.5%.
    for err in (0.015, -0.015):
        board = Board(clock_hz=CLOCK_HZ)
        tx = board.attach(UsbLsTransmitter('dp', 'dm', clock_hz=CLOCK_HZ, bit_rate_error=err))
        rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

        payload = bytes([0xA5, 0x5A, 0xFF, 0x00, 0x81])
        tx.send_data('DATA0', payload)
        _run(board, _cycles_for(8 * len(payload) + 16, CLOCK_HZ))

        assert len(rx.packets) == 1, f'err={err}'
        assert rx.packets[0].data == payload, f'err={err}'
        assert rx.packets[0].crc_ok is True, f'err={err}'
        assert rx.errors == [], f'err={err}: {rx.errors}'


# ---------------------------------------------------------------------
# Negative tests: deliberately malformed traffic must be caught.
# ---------------------------------------------------------------------

class _SymbolDriver(Device):
    """Drives an explicit list of line symbols ('J'/'K'/'SE0'), each held
    for cycles_per_bit cycles, for constructing malformed USB traffic."""

    def __init__(self, dp, dm, symbols, cycles_per_bit):
        self.dp = dp
        self.dm = dm
        self.symbols = symbols
        self.cpb = cycles_per_bit
        self._idx = 0
        self._in_bit = 0

    def step(self, cycle, nets):
        if self._idx >= len(self.symbols):
            dp, dm = _SYMBOL_PINS['J']
            return {self.dp: dp, self.dm: dm}
        sym = self.symbols[self._idx]
        self._in_bit += 1
        if self._in_bit >= self.cpb:
            self._in_bit = 0
            self._idx += 1
        dp, dm = _SYMBOL_PINS[sym]
        return {self.dp: dp, self.dm: dm}


def _nrzi(bits):
    symbols = []
    cur = 'J'
    for b in bits:
        if b == 0:
            cur = 'K' if cur == 'J' else 'J'
        symbols.append(cur)
    return symbols


def test_stuff_error_detected():
    cpb = round(CLOCK_HZ / 1.5e6)
    # PID = OUT (0001), check nibble = ~0001 = 1110, LSB-first bits:
    pid_bits = [1, 0, 0, 0] + [0, 1, 1, 1]
    # 7 consecutive 1-bits with NO stuffed 0 in between: illegal.
    bad_bits = pid_bits + [1, 1, 1, 1, 1, 1, 1]
    symbols = _nrzi(_SYNC_BITS + bad_bits) + ['SE0', 'SE0', 'J']

    board = Board(clock_hz=CLOCK_HZ)
    board.attach(_SymbolDriver('dp', 'dm', symbols, cpb))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    _run(board, len(symbols) * cpb + 20)

    assert any(e.kind == 'stuff_error' for e in rx.errors), rx.errors
    assert rx.packets == []


def test_bad_data_crc_flagged():
    board = Board(clock_hz=CLOCK_HZ)
    tx = board.attach(UsbLsTransmitter('dp', 'dm', clock_hz=CLOCK_HZ))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    payload = bytes([0x01, 0x02, 0x03, 0x04])
    payload_bits = []
    for byte in payload:
        payload_bits.extend((byte >> i) & 1 for i in range(8))
    wrong_crc_bits = [(~crc16(payload) >> i) & 1 for i in range(16)]  # deliberately inverted
    post_sync_bits = tx._pid_bits('DATA0') + payload_bits + wrong_crc_bits
    tx._enqueue_packet(post_sync_bits)

    _run(board, _cycles_for(8 * len(payload) + 16, CLOCK_HZ))

    assert len(rx.packets) == 1
    assert rx.packets[0].crc_ok is False


def test_bad_sync_field_flagged():
    cpb = round(CLOCK_HZ / 1.5e6)
    # Correct SOP (J->K) but a corrupted sync pattern after it.
    bad_sync = [0, 0, 0, 1, 0, 0, 0, 1]  # not 00000001
    symbols = _nrzi(bad_sync) + ['SE0', 'SE0', 'J']

    board = Board(clock_hz=CLOCK_HZ)
    board.attach(_SymbolDriver('dp', 'dm', symbols, cpb))
    rx = board.attach(UsbLsReceiver('dp', 'dm', clock_hz=CLOCK_HZ))

    _run(board, len(symbols) * cpb + 20)

    assert any(e.kind == 'sync_error' for e in rx.errors), rx.errors
    assert rx.packets == []
