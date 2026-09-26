"""Host library UART console and autobaud against the chip model (no RTL)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.uart import UartRx, UartTx  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host import uart as U  # noqa: E402


def board(*devs, pullups=()):
    h = Harness(None, boot="host", pullups=set(pullups))
    for d in devs:
        h.attach(d)
    return h, Link(model_transport(h))


@pytest.mark.parametrize("baud,bits,parity,stop,inv", [
    (115200, 8, None, 1, False),
    (1000000, 8, "even", 2, False),
    (57600, 7, "odd", 1, True),
])
def test_bridge_both_ways(baud, bits, parity, stop, inv):
    tgt_rx = UartRx("uio0", baud, bits=bits, parity=parity, stop=stop, invert=inv)
    tgt_tx = UartTx("uio1", baud, bits=bits, parity=parity, stop=stop, invert=inv, baud_error=0.01)
    h, link = board(tgt_rx, tgt_tx, pullups=() if inv else ("uio0", "uio1"))
    u = U.Uart(link, tx=0, rx=1, baud=baud, bits=bits, parity=parity, stop=stop,
               invert_tx=inv, invert_rx=inv)
    msg = bytes(c & ((1 << bits) - 1) for c in b"hello, board\r\n")
    u.write(msg)
    tgt_tx.send(msg[::-1])
    got = u.read(len(msg), timeout_us=400000)
    h.run_until(lambda: len(tgt_rx.bytes) >= len(msg), 50 * 50e6 / baud * 12 * len(msg))
    assert bytes(tgt_rx.bytes) == msg and not tgt_rx.errors, tgt_rx.errors[:3]
    assert got == msg[::-1]


def test_framing_and_parity_errors_reported():
    bad = UartTx("uio1", 115200, parity="odd")  # target sends odd parity, we expect even
    h, link = board(bad, pullups=("uio1",))
    u = U.Uart(link, baud=115200, parity="even")
    bad.send(b"\x01\x03")
    words = link.get(2, 200000)
    assert words[0] & U.PE and not words[1] & U.PE  # 0x01 has odd ones: parity bit differs


@pytest.mark.parametrize("baud,err", [(9600, 0.0), (115200, 0.012), (74880, 0.0), (460800, -0.01)])
def test_autobaud(baud, err):
    tx = UartTx("uio1", baud, baud_error=err)
    h, link = board(tx, pullups=("uio1",))
    tx.send(b"U\x00Boot 1.0\r\n" * 3, gap_bits=3)
    found, u = U.autobaud(link, rx=1, confirm=0)
    assert found == baud, found


def test_bridge_at_40mhz_chip_clock():
    """A die that needs a slower clock keeps exact rates: the library
    computes every bit time from Link.clock_hz."""
    tgt_rx = UartRx("uio0", 115200, clock_hz=40e6)
    tgt_tx = UartTx("uio1", 115200, clock_hz=40e6, baud_error=0.01)
    h = Harness(None, boot="host", pullups={"uio0", "uio1"}, clock_hz=40e6)
    h.attach(tgt_rx)
    h.attach(tgt_tx)
    link = Link(model_transport(h), clock_hz=40000000)
    u = U.Uart(link, tx=0, rx=1, baud=115200)
    assert abs(u.actual_baud - 115200) < 5
    u.write(b"40 MHz")
    tgt_tx.send(b"ok")
    assert u.read(2, timeout_us=400000) == b"ok"
    h.run_until(lambda: len(tgt_rx.bytes) >= 6, 400000)
    assert bytes(tgt_rx.bytes) == b"40 MHz" and not tgt_rx.errors
