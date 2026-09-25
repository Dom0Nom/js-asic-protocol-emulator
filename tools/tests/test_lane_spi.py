import pytest

from tempo.harness import Harness
from tempo.models.spi import SpiFlashLike


def spi_run(mode, per, words):
    src = f'.equ SPI_MODE, {mode}\n.equ SPI_PER, {per}\n.include "spi_master.s"\n'
    h = Harness(src)
    h.run(150)
    flash = h.attach(SpiFlashLike("uio2", "uio0", "uio1", "uio3", mode=mode,
                                  mem=bytearray(range(256)), min_setup_cycles=2, min_hold_cycles=2))
    h.chip.h2t.extend(words[:4])
    rest = list(words[4:])
    got = []
    for _ in range(4000):
        h.run(16)
        while h.chip.t2h:
            got.append(h.chip.t2h.popleft())
        while rest and len(h.chip.h2t) < 4:
            h.chip.h2t.append(rest.pop(0))
        if len(got) == len(words) and not h.chip.lanes[2].cur and not h.chip.lanes[2].queue:
            break
    h.run(4 * per)
    return h, flash, got


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_modes_jedec_and_read(mode):
    END = 0x8000
    words = [0x9F, 0, 0, END | 0,            # JEDEC id
             0x03, 0x00, 0x00, 0x10, 0, 0, END | 0]  # read 3 bytes at 0x10
    h, flash, got = spi_run(mode, 10, words)
    assert got[1:4] == [0xAA, 0x55, 0x01]
    assert got[8:11] == [0x10, 0x11, 0x12]
    assert len(flash.transactions) == 2
    assert not flash.violations
    assert not h.board.contention
