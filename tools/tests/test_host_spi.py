"""Host library SPI flash use case against the chip model (no RTL)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.spi import SpiNorFlash  # noqa: E402
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
    h, f, fl = board(mode=3, period=6)
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
