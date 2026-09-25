import pytest

from tempo.harness import Harness
from tempo.models.i2c import I2cBusMonitor, I2cTarget

START, STOP, WR, RDA, RDN = 1, 2, 3, 4, 5


def i2c_run(cmds, mode="standard", stretch=0, address=0x50, nack_after=None):
    slot = 500 if mode == "standard" else 125
    h = Harness(f'.equ I2C_SLOT, {slot}\n.include "i2c_master.s"\n', pullups={"uio4", "uio5"})
    regs = bytearray(range(0x80, 0x80 + 256 - 128)) + bytearray(128)
    tgt = h.attach(I2cTarget("uio5", "uio4", address, regs=regs, mode=mode,
                             stretch_cycles=stretch, nack_after=nack_after))
    mon = h.attach(I2cBusMonitor("uio5", "uio4", mode=mode))
    pending = [c << 8 | d for c, d in cmds]
    replies = []
    for _ in range(20000):
        while pending and len(h.chip.h2t) < 4:
            h.chip.h2t.append(pending.pop(0))
        h.run(50)
        while h.chip.t2h:
            replies.append(h.chip.t2h.popleft())
        lane = h.chip.lanes[3]
        if not pending and not h.chip.h2t and lane.cur is None and not lane.queue:
            break
    h.run(slot * 2)
    return h, tgt, mon, replies


@pytest.mark.parametrize("mode", ["standard", "fast"])
@pytest.mark.parametrize("stretch", [0, 300])
def test_register_write_then_read_with_repeated_start(mode, stretch):
    cmds = [(START, 0), (WR, 0x50 << 1), (WR, 0x10), (WR, 0xAB), (WR, 0xCD),
            (START, 0), (WR, 0x50 << 1), (WR, 0x10),
            (START, 0), (WR, 0x50 << 1 | 1), (RDA, 0), (RDN, 0), (STOP, 0)]
    h, tgt, mon, replies = i2c_run(cmds, mode, stretch)
    assert replies == [0, 0, 0, 0, 0, 0, 0, 0xAB, 0xCD]
    assert tgt.regs[0x10:0x12] == b"\xab\xcd"
    assert not tgt.violations and not mon.violations, (tgt.violations + mon.violations)[:5]
    assert not h.board.contention


def test_nack_on_wrong_address():
    h, tgt, mon, replies = i2c_run([(START, 0), (WR, 0x23 << 1), (STOP, 0)])
    assert replies == [1]
    assert not mon.violations
