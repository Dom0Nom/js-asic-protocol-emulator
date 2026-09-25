# SPDX-License-Identifier: Apache-2.0
"""Use-case tests: the host library (host/tempo_host) drives the RTL through
the host SPI port, in lockstep with the model, against device models."""

import cocotb

from lockstep import Lockstep
from tempo.models.i2c import Eeprom24Target, I2cTarget, StuckSda
from tempo.transport import cocotb_call, cocotb_transport

from tempo_host.link import Link
from tempo_host import i2c as i2c_mod


async def setup(dut, pullups=()):
    ls = Lockstep(dut, None, pullups=pullups)
    await ls.start()
    link = Link(cocotb_transport(ls))
    return ls, link


@cocotb.test()
async def test_uc2_i2c_bringup(dut):
    """Use case 2: scan, register dump, 24C02 page write + ACK polling, recovery."""
    ls, link = await setup(dut, pullups={"uio4", "uio5"})
    sensor = ls.attach(I2cTarget("uio5", "uio4", 0x68, regs=bytearray(range(256)), mode="fast",
                                 stretch_cycles=30))
    ee = ls.attach(Eeprom24Target("uio5", "uio4", size=256, page=8, abytes=1, t_wr=40e-6, mode="fast"))

    def host():
        bus = i2c_mod.I2c(link, sda=4, scl=5, mode="fm")
        found = bus.scan(0x48, 0x6F)  # full range runs in the model test
        regs = bus.read_regs(0x68, 0x10, 4)
        rom = i2c_mod.Eeprom24(bus, "24c02")
        rom.write(0x05, bytes(range(0x40, 0x54)))
        back = rom.read(0x05, 20)
        return found, regs, back

    found, regs, back = await cocotb_call(host)
    assert found == [0x50, 0x68], found
    assert regs == bytes([0x10, 0x11, 0x12, 0x13])
    assert back == bytes(range(0x40, 0x54))
    assert ee.writes == 4
    assert not sensor.violations and not ee.violations, (sensor.violations[:3], ee.violations[:3])
    assert not ls.board.contention
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_uc2_i2c_recovery(dut):
    ls, link = await setup(dut, pullups={"uio4", "uio5"})
    ls.attach(StuckSda("uio5", "uio4", release_after=6))

    def host():
        bus = i2c_mod.I2c(link, sda=4, scl=5, mode="sm")
        return bus.recover()

    assert await cocotb_call(host) == 6
