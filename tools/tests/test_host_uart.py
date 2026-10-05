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
    # Odd and even parity differ for every data value. Previously the
    # startup race sampled these as 0x80/0xe0, accidentally passing a
    # check that only the first word had PE and never checked the data.
    assert words == [U.PE | 0x01, U.PE | 0x03]


@pytest.mark.parametrize("baud,err", [(9600, 0.0), (115200, 0.012), (74880, 0.0), (230400, -0.01)])
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


def test_fractional_baud_rounding_carries_into_whole_bit():
    # 50,999,999 / 3,000,000 is just below 17; rounding its fraction
    # to 256 must produce a 17-cycle bit, rather than truncate to 16.
    pre, whole, frac = U.bit_time(3000000, 50999999)
    assert (whole + frac / 256) * (1 << pre) == 17


def test_bit_time_keeps_midpoint_in_13_bit_sample_register():
    _, whole, _ = U.bit_time(300)
    assert whole // 2 <= 8191


@pytest.mark.parametrize("baud", [300, 600, 1200, 2400])
def test_slow_baud_receive_uses_representable_sample_offset(baud):
    tx = UartTx("uio1", baud, baud_error=-0.02)
    h, link = board(tx, pullups=("uio1",))
    u = U.Uart(link, baud=baud)
    h.run(1000)  # isolate sample-offset truncation from the startup race
    tx.send(b"\x81")
    assert u.read(1, timeout_us=50000) == b"\x81"
    assert abs(u.actual_baud / baud - 1) < 0.001


@pytest.mark.parametrize("parity", [None, "even", "odd"])
def test_receive_is_armed_when_constructor_returns(parity):
    tx = UartTx("uio1", 115200, parity=parity)
    h, link = board(tx, pullups=("uio1",))
    u = U.Uart(link, baud=115200, parity=parity)
    tx.send(b"\x01\x03\x81")
    assert u.read(3, timeout_us=2000) == b"\x01\x03\x81"


def test_loading_uart_releases_previous_spi_pin_owners():
    from tempo_host.spi import Spi
    h, link = board(pullups=("uio1",))
    spi = Spi(link, period=6)
    spi.write(b"\x00")
    spi.end()
    U.Uart(link)
    link.check()  # lane 2 must not still own UART's TX pin


def test_loading_uart_discards_commands_for_previous_firmware():
    from tempo_host.spi import Spi
    rx = UartRx("uio4", 115200)
    h, link = board(rx, pullups=("uio1", "uio4"))
    Spi(link, period=6)
    link.halt()
    link.put([1, 0x5500])  # queued for the old image while halted
    u = U.Uart(link, tx=4)
    u.write(b"\x81")
    h.run(15000)
    assert rx.bytes == [0x81] and not rx.errors, (rx.bytes, rx.errors)


def test_reloading_uart_does_not_forward_old_ring_contents():
    tx = UartTx("uio1", 115200)
    h, link = board(tx, pullups=("uio1",))
    u = U.Uart(link)
    tx.send(b"\x81\x82\x83")
    assert u.read(3, timeout_us=2000) == b"\x81\x82\x83"
    u = U.Uart(link)
    tx.send(b"\x99")
    assert u.read(1, timeout_us=2000) == b"\x99"


def test_uart_terminal_overflow_reports_pending_loss_on_recovery():
    """A full ring followed by silence has no later word to carry LOST."""
    tx = UartTx("uio1", 1000000)
    h, link = board(tx, pullups=("uio1",))
    u = U.Uart(link, baud=1000000)
    sent = bytes((i * 13 + 7) & 255 for i in range(1024))
    tx.send(sent)
    # Do not poll while twice the SRAM ring capacity arrives. Then leave
    # ample idle time, so a marker cannot be supplied by another character.
    h.run(len(sent) * 10 * 50 + 1000)
    words = []
    for _ in range(160):
        chunk = u.read_raw()
        if not chunk:
            break
        words.extend(chunk)
    else:
        pytest.fail("UART ring did not drain")
    assert 0 < len(words) < len(sent)
    assert bytes(w & 255 for w in words) == sent[:len(words)]
    assert not any(w & (U.FE | U.PE | U.LOST) for w in words)
    # Existing firmware retains the pending loss internally during silence.
    # The first successfully stored recovery character reports that gap.
    tx.send(b"\xa5\x3c")
    assert link.get(2, timeout_us=20000) == [U.LOST | 0xA5, 0x3C]
    link.check()
