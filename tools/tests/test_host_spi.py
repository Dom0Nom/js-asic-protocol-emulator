"""Host library SPI flash use case against the chip model (no RTL)."""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.spi import SpiNorFlash, SpiSlave  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host.spi import Spi, SpiFlash  # noqa: E402


def board(mode=0, period=8, **kw):
    h = Harness(None, boot="host")
    f = h.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=mode, t_pp=20e-6, t_se=60e-6,
                             min_setup_cycles=2, min_hold_cycles=1, **kw))
    link = Link(model_transport(h))
    return h, f, SpiFlash(Spi(link, mode=mode, period=period))


def test_id_sfdp_read():
    mem = bytearray(os.urandom(1 << 16))
    h, f, fl = board(mem=mem)
    assert fl.jedec_id() == (0xEF, 0x40, 0x18)
    info = fl.sfdp_info()
    assert info["size"] == 1 << 16 and info["erase_4k_opcode"] == 0x20
    assert fl.read(0x1234, 300) == bytes(mem[0x1234:0x1234 + 300])
    assert not f.violations


def test_erase_program_verify_mode3():
    h, f, fl = board(mode=3, period=7)
    fl.erase_sector(0x2000)
    data = bytes((i * 7) & 0xFF for i in range(300))
    fl.program(0x20F0, data)  # crosses a page boundary: 16 + 256 + 28 bytes
    assert f.programs == 3 and f.erases == 1
    assert bytes(f.mem[0x20F0:0x20F0 + 300]) == data
    assert not f.violations, f.violations[:3]


def test_bulk_read_through_sram():
    mem = bytearray(os.urandom(1 << 16))
    h, f, fl = board(period=6, mem=mem)
    spi = fl.spi
    spi.write(b"\x03\x00\x10\x00")
    got = spi.read_bulk(2048)
    spi.end()
    assert got == bytes(mem[0x1000:0x1800])
    assert not f.violations


@pytest.mark.parametrize("mode,period", [(0, 5), (1, 5), (1, 6), (2, 5), (3, 6)])
def test_reject_spi_period_that_samples_previous_bit(mode, period):
    h = Harness(None, boot="host")
    with pytest.raises(ValueError):
        Spi(Link(model_transport(h)), mode=mode, period=period)


@pytest.mark.parametrize("kw", [{"mode": -1}, {"mode": 4}, {"period": 8192},
                                {"sample": -1}, {"sample": 4}, {"sample": 8}])
def test_reject_invalid_spi_timing_before_loading(kw):
    h = Harness(None, boot="host")
    link = Link(model_transport(h))
    with pytest.raises(ValueError):
        Spi(link, **kw)
    assert link.image is None


@pytest.mark.parametrize("mode,period", [(0, 6), (1, 7), (2, 6), (3, 7),
                                        (0, 7), (1, 7), (2, 8), (3, 8)])
def test_full_duplex_at_minimum_period_in_every_mode(mode, period):
    h = Harness(None, boot="host", pullups={"uio3"})
    slave = h.attach(SpiSlave("uio2", "uio0", "uio1", "uio3", mode=mode,
                             response=b"\xa5\x3c\x96\x69"))
    spi = Spi(Link(model_transport(h)), mode=mode, period=period)
    data = b"\x81\x3c\xa5\x7e\x00\xff\x96\x69\x42"
    assert spi.xfer(data) == (b"\xa5\x3c\x96\x69" * 3)[:len(data)]
    spi.end()
    h.run(300)
    assert slave.transactions[-1][0] == data
    assert not slave.violations, slave.violations
    assert not h.board.contention


def test_bulk_read_overlaps_next_transfer_with_sram_store():
    mem = bytearray((i * 7 + 3) & 0xFF for i in range(1 << 16))
    h, f, fl = board(period=6, mem=mem)
    spi = fl.spi
    spi.write(b"\x03\x00\x10\x00")
    start = h.board.cycle
    got = spi.read_bulk(1024)
    elapsed = h.board.cycle - start
    spi.end()
    h.run(300)
    assert got == bytes(mem[0x1000:0x1400])
    # The previous tx/rx/store loop takes 132,889 cycles on this board.
    # Require a real throughput gain with identical host-port timing.
    assert elapsed < 128000, elapsed
    assert not f.violations


@pytest.mark.parametrize("mode", range(4))
def test_lsb_first_preserves_byte_order_and_odd_tail(mode):
    h = Harness(None, boot="host", pullups={"uio3"})
    slave = h.attach(SpiSlave("uio2", "uio0", "uio1", "uio3", mode=mode,
                             msb_first=False, response=b"\x96\xa5\x3c\x69"))
    spi = Spi(Link(model_transport(h)), mode=mode, period=8, msb_first=False)
    data = b"\x81\x3c\xa5\x7e\x00\xff\x96\x69\x42"
    assert spi.xfer(data) == (b"\x96\xa5\x3c\x69" * 3)[:len(data)]
    spi.end()
    h.run(300)
    assert slave.transactions[-1][0] == data
    spi.write(data[:3])
    assert spi.read(3) == b"\x69\x96\xa5"
    spi.end()
    h.run(300)
    assert slave.transactions[-1][0] == data[:3] + b"\x00" * 3
    assert not slave.violations
    assert not h.board.contention


def test_lsb_first_bulk_read_preserves_order_across_sram_halves():
    h = Harness(None, boot="host", pullups={"uio3"})
    response = b"\x96\xa5\x3c\x69"
    slave = h.attach(SpiSlave("uio2", "uio0", "uio1", "uio3", mode=3,
                             msb_first=False, response=response))
    spi = Spi(Link(model_transport(h)), mode=3, period=7, msb_first=False)
    assert spi.read_bulk(1024) == response * 256
    spi.end()
    h.run(300)
    assert slave.transactions[-1][0] == bytes(1024)
    assert not slave.violations
    assert not h.board.contention
