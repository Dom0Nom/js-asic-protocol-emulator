# SPDX-License-Identifier: Apache-2.0
"""PS/2 on the RTL: fw/ps2.s in lockstep with the cycle model, both roles
checked by the independent wire models (tools/tempo/models/ps2.py)."""

import cocotb

from lockstep import Lockstep
from tempo.clock import CLOCK_HZ
from tempo.models.ps2 import Ps2Device, Ps2Host


@cocotb.test()
async def test_ps2_device_and_host(dut):
    ls = Lockstep(dut, f'.equ PS2_CLK_KHZ, {CLOCK_HZ // 1000}\n.include "ps2.s"\n',
                  pullups={"uio0", "uio1", "uio2", "uio3"})
    await ls.start()
    await ls.boot()
    await ls.run(200)
    host = ls.attach(Ps2Host("uio1", "uio0"))
    dev = ls.attach(Ps2Device("uio3", "uio2", period_us=60, setup_us=5))
    ref = ls.attach(Ps2Host("uio3", "uio2"))

    sent = [0x1C, 0xFF, 0x00]
    dev.send([0xAA, 0x00])
    dev.send([0x5B], bad_parity=True)
    await ls.mbox_write(sent)
    frame = 11 * 100 * CLOCK_HZ // 1_000_000
    done = await ls.run_until(lambda: len(host.bytes) == 3 and len(ref.bytes) == 3, 5 * frame)
    assert done, (host.bytes, ref.bytes)
    await ls.run(frame // 4)
    got = await ls.mbox_read(3)

    assert host.bytes == sent, [hex(b) for b in host.bytes]
    assert not host.violations, host.violations[:4]
    assert got == [0xAA, 0x00, 0x100 | 0x5B], [hex(w) for w in got]
    assert [v.kind for v in ref.violations] == ["parity"]
    assert not ls.board.contention, ls.board.contention[:4]
    assert ls.chip.lanes[1].stat == 0 and ls.chip.lanes[2].stat == 0
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")
