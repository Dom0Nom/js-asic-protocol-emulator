"""Host library SWD debug probe against the chip model (no RTL)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.swd import SwdTarget  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host import swd as S  # noqa: E402


def board(**kw):
    h = Harness(None, boot="host", pullups={"uio0"})
    t = h.attach(SwdTarget("uio1", "uio0", **kw))
    return h, t, S.Swd(Link(model_transport(h)), swdio=0, swclk=1, period=10)


def test_attach_and_rom_table():
    h, t, p = board(wait_count=3, pwrup_delay=200)
    dp = p.connect(from_jtag=True)
    assert dp["dpidr"] == 0x2BA01477 and dp["designer"] == "Arm"
    p.power_up()
    rom = p.rom_table()
    assert rom["idr"] == 0x24770011 and rom["base"] == 0xE00FF000 and rom["rom_cid_ok"]
    assert [e["address"] for e in rom["entries"]] == [0xE000E000, 0xE0001000]
    assert p.waits == 3
    assert not t.violations and not h.board.contention


def test_fault_is_reported_and_cleared():
    h, t, p = board(fault_addrs={0x20000000}, start_in_jtag=False)
    p.connect(from_jtag=False)
    p.power_up()
    with pytest.raises(S.SwdFault):
        p.mem_read(0x20000000)  # sets STICKYERR; the next AP access faults
        p.mem_read(0x20000004)
    p.clear_errors()
    assert p.mem_read(0xE00FF000) & 3 == 3
    assert not t.violations
