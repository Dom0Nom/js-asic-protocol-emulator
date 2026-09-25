# SPDX-License-Identifier: Apache-2.0
"""Use-case tests: the host library (host/tempo_host) drives the RTL through
the host SPI port, in lockstep with the model, against device models."""

import cocotb

from lockstep import Lockstep
from tempo.models.i2c import Eeprom24Target, I2cTarget, StuckSda
from tempo.transport import cocotb_call, cocotb_transport

from tempo.models.spi import SpiNorFlash
from tempo.models.uart import UartRx, UartTx
from tempo_host.link import Link
from tempo_host import i2c as i2c_mod
from tempo_host import spi as spi_mod
from tempo_host import uart as uart_mod


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


@cocotb.test()
async def test_uc3_spi_flash_programmer(dut):
    """Use case 3: JEDEC ID, SFDP, read, bulk read through SRAM, erase, program."""
    import random
    ls, link = await setup(dut)
    rng = random.Random(3)
    mem = bytearray(rng.getrandbits(8) for _ in range(1 << 16))
    flash = ls.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=0, mem=mem, t_pp=20e-6,
                                  t_se=60e-6, min_setup_cycles=2, min_hold_cycles=1))

    def host():
        f = spi_mod.SpiFlash(spi_mod.Spi(link, mode=0, period=6))
        ident = f.jedec_id()
        info = f.sfdp_info()
        head = f.read(0x0100, 64)
        f.spi.write(b"\x03\x00\x10\x00")
        bulk = f.spi.read_bulk(1024)
        f.spi.end()
        f.erase_sector(0x3000)
        f.program(0x30F0, bytes(range(40)))  # crosses a page boundary
        return ident, info, head, bulk

    ident, info, head, bulk = await cocotb_call(host)
    assert ident == (0xEF, 0x40, 0x18)
    assert info["size"] == 1 << 16
    assert head == bytes(mem[0x100:0x140])
    assert bulk == bytes(mem[0x1000:0x1400])
    assert bytes(flash.mem[0x30F0:0x3118]) == bytes(range(40))
    assert flash.erases == 1 and flash.programs == 2
    assert not flash.violations, flash.violations[:3]
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_uc1_uart_autobaud_console(dut):
    """Use case 1: autobaud on an undocumented debug UART (115200 +1%),
    confirm on framed characters, then bridge both ways."""
    ls, link = await setup(dut, pullups={"uio0", "uio1"})
    target_tx = ls.attach(UartTx("uio1", 115200, baud_error=0.01))
    target_rx = ls.attach(UartRx("uio0", 115200))
    target_tx.send(b"U-Boot 2026.01\r\n", gap_bits=2)

    def host():
        baud, u = uart_mod.autobaud(link, rx=1, tx=0, confirm=0)
        u.flush()
        target_tx.send(b"ok>")
        prompt = u.read(3)
        u.write(b"help\r")
        return baud, prompt

    baud, prompt = await cocotb_call(host)
    await ls.run(434 * 10 * 6)
    assert baud == 115200, baud
    assert prompt == b"ok>", prompt
    assert bytes(target_rx.bytes) == b"help\r" and not target_rx.errors, target_rx.errors
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")
