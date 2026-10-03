import pytest

from tempo.harness import Harness
from tempo.models.usb_ls import UsbLsReceiver, UsbLsTransmitter

PIDS = {"OUT": 0x1, "IN": 0x9, "SETUP": 0xD, "DATA0": 0x3, "DATA1": 0xB, "ACK": 0x2, "NAK": 0xA}


def pid_byte(name):
    p = PIDS[name]
    return p | ((~p & 0xF) << 4)


CLOCKS = [25e6, 50e6]  # declared clock first; 50 MHz shows the bit length follows USB_CLK_KHZ


def bits(clock_hz, n):
    """Cycles for n USB-LS bit times, rounded up."""
    return int(n * clock_hz / 1.5e6) + 1


def harness(clock_hz):
    h = Harness(f'.equ USB_CLK_KHZ, {int(clock_hz // 1000)}\n.include "usb_ls_phy.s"\n',
                clock_hz=clock_hz, pullups={"uio6"})
    h.run(300)
    return h


def drain(h, cycles):
    out = []
    for _ in range(int(cycles // 50)):
        h.run(50)
        while h.chip.t2h:
            out.append(h.chip.t2h.popleft())
    return out


@pytest.mark.parametrize("clock_hz", CLOCKS)
def test_usb_tx_data_and_handshake(clock_hz):
    h = harness(clock_hz)
    rx = h.attach(UsbLsReceiver("uio7", "uio6", clock_hz=clock_hz))
    data = [0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x12, 0x00]  # a GET_DESCRIPTOR setup payload
    h.chip.h2t.extend([0x8000 | len(data) << 8 | pid_byte("DATA0")] + data[:3])
    for b in data[3:]:
        h.run_until(lambda: len(h.chip.h2t) < 4, 20000)
        h.chip.h2t.append(b)
    h.run_until(lambda: not h.chip.h2t, 20000)
    h.run(bits(clock_hz, 40))
    h.chip.h2t.append(pid_byte("ACK"))
    h.run(bits(clock_hz, 40))
    assert [p.pid for p in rx.packets] == ["DATA0", "ACK"], (rx.packets, rx.errors)
    assert list(rx.packets[0].data) == data and rx.packets[0].crc_ok
    assert not rx.errors
    assert h.chip.lanes[0].stat == 0  # no LATE, UNDER or other sticky flags


def unpack(words):
    """Split the PHY's packet output into (bytes, end word) pairs."""
    pkts, cur = [], []
    for w in words:
        if w & 0xE000 == 0xE000 and len(cur) == ((w >> 4 & 15) + 1) // 2:
            n = w >> 4 & 15
            b = [x for word in cur for x in (word & 0xFF, word >> 8)][:n]
            pkts.append((b, w))
            cur = []
        else:
            cur.append(w)
    return pkts


@pytest.mark.parametrize("clock_hz", CLOCKS)
def test_usb_rx_token_and_data_with_clock_error(clock_hz):
    for err in (-0.015, 0.0, 0.015):
        for data in ([0xFF, 0xFF, 0xFF, 0x3F, 0x00, 0x42], [0xFF, 0xFF, 0x7E, 0x00, 0x81]):
            h = harness(clock_hz)
            tx = h.attach(UsbLsTransmitter("uio7", "uio6", clock_hz=clock_hz, bit_rate_error=err))
            tx.send_token("SETUP", addr=0, endp=0)
            tx.send_data("DATA0", bytes(data))  # long runs of ones: stuffing across bytes
            pkts = unpack(drain(h, bits(clock_hz, 160)))
            assert len(pkts) == 2, (err, pkts)
            tok, end1 = pkts[0]
            assert tok == [0x80, pid_byte("SETUP"), 0x00, 0x10], (err, tok)  # CRC5 0x02 in the top bits
            assert end1 & 0x10F == 0  # whole bytes; token has no CRC16
            dat, end2 = pkts[1]
            assert dat[:2 + len(data)] == [0x80, pid_byte("DATA0")] + data, (err, dat)
            assert end2 & 0x100, (err, hex(end2))  # CRC16 residual ok
            assert h.chip.lanes[1].stat & ~((1 << 3) | (1 << 4)) == 0  # only pattern events
            assert not h.board.contention
