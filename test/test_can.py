# SPDX-License-Identifier: Apache-2.0
"""CAN transmit firmware on the RTL, in lockstep with the cycle-level model,
checked on the wire by the independent CAN nodes in tempo.models.can."""

import cocotb

from lockstep import Lockstep
from tempo.clock import CLOCK_HZ
from tempo.models.can import CanFrame, CanReceiver, CanTransmitter, stuff_positions

BUS = "uio0"
KBPS = 500
ACKED, LOST = 0, 2


def words(frame):
    d = list(frame.data)
    return [frame.ident << 4 | len(d)] + [d[k] << 8 | d[k + 1] for k in range(0, len(d) - 1, 2)] + d[len(d) & ~1:]


@cocotb.test()
async def test_can_tx_acknowledged_frame_and_lost_arbitration(dut):
    bit = CLOCK_HZ / (KBPS * 1000)
    node = dict(bit_rate=KBPS * 1000, clock_hz=CLOCK_HZ)
    ls = Lockstep(dut, f'.equ CAN_CLK_KHZ, {CLOCK_HZ // 1000}\n.equ CAN_KBPS, {KBPS}\n.include "can_tx.s"\n',
                  pullups={BUS})
    await ls.start()
    await ls.boot()
    rx = ls.attach(CanReceiver(BUS, **node))
    await ls.run(12 * bit)

    frame = CanFrame(0x13C, bytes(range(1, 9)))  # stuff bits in the data and after the last CRC bit
    assert len(stuff_positions(frame)) == 10
    await ls.mbox_write(words(frame))
    assert await ls.mbox_read(1) == [ACKED]
    assert rx.frames == [frame] and rx.frames[0].stuff_bits == 10 and rx.frames[0].acked

    mine, theirs = CanFrame(0x123, b"\x12\x34\x56"), CanFrame(0x122, b"\xCA\xFE")
    other = ls.attach(CanTransmitter(BUS, theirs, **node))
    await ls.mbox_write(words(mine))
    assert await ls.mbox_read(1) == [LOST]
    for _ in range(int(150 * bit)):
        assert not int(dut.uio_oe.value) & 1, "bus driven after losing arbitration"
        if other.result is not None:
            break
        await ls.cycle()
    assert other.result == "sent" and other.acked
    assert rx.frames == [frame, theirs]
    assert not rx.violations and not other.violations, (rx.violations, other.violations)
    assert not ls.board.contention
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")
