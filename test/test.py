# SPDX-License-Identifier: Apache-2.0
"""RTL tests: every test runs the RTL in lockstep with the cycle-level model
(test/lockstep.py) and checks the wire with an independent protocol model."""

import cocotb

from lockstep import Lockstep
from tempo.models.uart import UartRx
from tempo.models.spi import SpiFlashLike
from tempo.models.i2c import I2cTarget

TX_SRC = """
        .include "tempo.inc"
        .equ PER, 16
        .equ FRACV, 171                ; 50 MHz / 3 Mbaud = 16.67 cycles
        .entry 0, main
main:   lc   r1, (NOPIN << 12) | (NOPIN << 8) | (NOPIN << 4) | UO0
        lw   l0, pins, r1
        lc   r1, PER
        lw   l0, len0, r1
        lw   l0, len1, r1
        lc   r1, FRACV
        lw   l0, frac, r1
        lc   r1, SYM_NRZ1
        lw   l0, sym1, r1
        li   r1, E_D | E_DEN
        emit l0, r1
        lc   r3, 0x0200
loop:   hget r4
        shli r4, r4, 1
        or   r4, r4, r3
        tx   l0, r4, 10
        bra  loop
"""


@cocotb.test()
async def test_host_boot_and_uart_tx(dut):
    ls = Lockstep(dut, TX_SRC)
    await ls.start()
    await ls.boot()
    await ls.run(100)  # firmware sets the line idle; uo0 is low from reset until then
    rx = ls.attach(UartRx("uo0", 3_000_000))
    data = [0x55, 0x00, 0xFF, 0xA5, 0x3C]
    ls.h.host.mbox_write(data)
    await ls.boot()
    await ls.run(17 * 10 * (len(data) + 1))
    assert rx.bytes == data, rx.bytes
    assert not rx.errors
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_spi_mode3_flash_read(dut):
    ls = Lockstep(dut, '.equ SPI_MODE, 3\n.equ SPI_PER, 10\n.include "spi_master.s"\n')
    await ls.start()
    await ls.boot()
    flash = ls.attach(SpiFlashLike("uio2", "uio0", "uio1", "uio3", mode=3,
                                   mem=bytearray(range(256)), min_setup_cycles=2, min_hold_cycles=2))
    END = 0x8000
    ls.h.host.mbox_write([0x03, 0x00, 0x00])
    ls.h.host.mbox_write([0x20, 0, END | 0])
    await ls.boot()
    await ls.run(600)
    got = ls.h.host.mbox_read(6)  # cmd, 3 address bytes, 2 data bytes
    await ls.boot()
    assert got.words(2)[4:6] == [0x20, 0x21], got.words(2)
    assert not flash.violations


@cocotb.test()
async def test_i2c_write_read_with_stretching(dut):
    ls = Lockstep(dut, '.equ I2C_SLOT, 125\n.include "i2c_master.s"\n', pullups={"uio4", "uio5"})
    await ls.start()
    tgt = ls.attach(I2cTarget("uio5", "uio4", 0x50, mode="fast", stretch_cycles=40))
    await ls.boot()
    START, STOP, WR, RDN = 1, 2, 3, 5
    cmds = [(START, 0), (WR, 0xA0), (WR, 0x10), (WR, 0x5A), (START, 0), (WR, 0xA0), (WR, 0x10),
            (START, 0), (WR, 0xA1), (RDN, 0), (STOP, 0)]
    ls.h.host.mbox_write([c << 8 | d for c, d in cmds[:4]])
    ls.h.host.mbox_write([c << 8 | d for c, d in cmds[4:8]])
    ls.h.host.mbox_write([c << 8 | d for c, d in cmds[8:]])
    await ls.boot()
    await ls.run(8000)
    rep = ls.h.host.mbox_read(4)
    await ls.boot()
    assert rep.words(2)[:4] == [0, 0, 0, 0]
    assert tgt.regs[0x10] == 0x5A
    assert not tgt.violations, tgt.violations[:4]


@cocotb.test()
async def test_usb_ls_rx_and_tx(dut):
    from tempo.models.usb_ls import UsbLsReceiver, UsbLsTransmitter
    ls = Lockstep(dut, '.include "usb_ls_phy.s"\n', pullups={"uio6"})
    await ls.start()
    await ls.boot()
    await ls.run(300)
    tx = ls.attach(UsbLsTransmitter("uio7", "uio6", bit_rate_error=0.015))
    tx.send_data("DATA1", bytes([0xFF, 0xFF, 0x7E, 0x00, 0x81]))
    await ls.run(34 * 90)
    got = ls.h.host.mbox_read(6)  # 9 bytes in 5 words, then the end word
    await ls.boot()
    words = got.words(2)
    b = [x for w in words[:5] for x in (w & 0xFF, w >> 8)][:9]
    assert b[:7] == [0x80, 0x4B, 0xFF, 0xFF, 0x7E, 0x00, 0x81], [hex(w) for w in words]
    assert words[5] == 0xE000 | 0x100 | 9 << 4, [hex(w) for w in words]  # residual ok, 9 bytes
    # answer with ACK through lane 0; the model checks the waveform
    rx = ls.attach(UsbLsReceiver("uio7", "uio6"))
    ls.h.host.mbox_write([0xD2])  # ACK PID byte, no data, no CRC
    await ls.boot()
    await ls.run(34 * 30)
    assert [p.pid for p in rx.packets][-1] == "ACK", (rx.packets, rx.errors)
    assert not rx.errors
