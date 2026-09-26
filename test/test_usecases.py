# SPDX-License-Identifier: Apache-2.0
"""Use-case tests: the host library (host/tempo_host) drives the RTL through
the host SPI port, in lockstep with the model, against device models."""

import cocotb

from lockstep import Lockstep
from tempo.models.i2c import Eeprom24Target, I2cTarget, StuckSda
from tempo.transport import cocotb_call, cocotb_transport

from tempo.models.spi import SpiNorFlash, SpiSlave
from tempo.models.uart import UartRx, UartTx
from tempo.models.swd import SwdTarget
from tempo.models.jtag import JtagChain, Tap
from tempo.models.siggen import Pulses, Square
from tempo_host.link import Link
from tempo_host import i2c as i2c_mod
from tempo_host import spi as spi_mod
from tempo_host import uart as uart_mod
from tempo_host import swd as swd_mod
from tempo_host import jtag as jtag_mod
from tempo_host.la import LogicAnalyser
from tempo_host.trigger import PulseTrigger


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
async def test_i2c_stops_at_data_nack(dut):
    for addr, addr10 in [(0x68, False), (0x2A5, True)]:
        ls, link = await setup(dut, pullups={"uio4", "uio5"})
        target = ls.attach(I2cTarget("uio5", "uio4", addr, mode="fast",
                                     nack_after=0, addr10=addr10))

        def host():
            bus = i2c_mod.I2c(link)
            try:
                bus.transfer(addr, b"\x20\xa1\xb2\xc3", read=2, addr10=addr10)
            except i2c_mod.Nack:
                return bus
            raise AssertionError("target NACK was ignored")

        bus = await cocotb_call(host)
        await ls.run(2000)
        assert [e.detail for e in target.events if e.kind == "data_write_rejected"] == [0xA1]
        assert target.events[-1].kind == "stop"

        def next_transfer():
            target.nack_after = None
            bus.transfer(addr, b"\x20\x99", addr10=addr10)
            return bus.transfer(addr, b"\x20", 1, addr10=addr10)

        assert await cocotb_call(next_transfer) == b"\x99"
        assert not target.violations and not ls.board.contention


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
async def test_spi_all_modes_at_minimum_period(dut):
    """Host firmware, real target model and pad/commit lockstep, including
    an odd byte and a host transfer boundary with CS held low."""
    for mode, msb_first in [(mode, order) for mode in range(4) for order in (True, False)]:
        ls, link = await setup(dut, pullups={"uio3"})
        slave = ls.attach(SpiSlave("uio2", "uio0", "uio1", "uio3", mode=mode,
                                  msb_first=msb_first,
                                  response=b"\xa5\x3c\x96\x69"))
        data = b"\x81\x3c\xa5\x7e\x00\xff\x96\x69\x42"

        def host():
            spi = spi_mod.Spi(link, mode=mode, period=5 + (mode & 1), msb_first=msb_first)
            got = spi.xfer(data)
            spi.end()
            return got

        assert await cocotb_call(host) == (b"\xa5\x3c\x96\x69" * 3)[:len(data)]
        await ls.run(300)
        assert slave.transactions[-1][0] == data
        assert not slave.violations, (mode, slave.violations)
        assert not ls.board.contention
        await cocotb_call(link.check)


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


@cocotb.test()
async def test_uart_receive_ready_and_slow_baud(dut):
    for baud, parity in [(115200, None), (115200, "even"), (115200, "odd"), (300, None)]:
        ls, link = await setup(dut, pullups={"uio1"})
        tx = ls.attach(UartTx("uio1", baud, parity=parity,
                               baud_error=-0.02 if baud == 300 else 0))

        def host():
            u = uart_mod.Uart(link, baud=baud, parity=parity)
            tx.send(b"\x81")  # starts immediately when construction returns
            return u.read(1, timeout_us=50000)

        assert await cocotb_call(host) == b"\x81"
        assert not ls.board.contention


@cocotb.test()
async def test_uart_parity_error_preserves_received_data(dut):
    ls, link = await setup(dut, pullups={"uio1"})
    tx = ls.attach(UartTx("uio1", 115200, parity="odd"))

    def host():
        uart_mod.Uart(link, baud=115200, parity="even")
        tx.send(b"\x01\x03")
        return link.get(2, timeout_us=2000)

    words = await cocotb_call(host)
    assert words == [uart_mod.PE | 0x01, uart_mod.PE | 0x03], words


@cocotb.test()
async def test_firmware_reload_clears_pin_owners_modes_and_mailboxes(dut):
    ls, link = await setup(dut, pullups={"uio1", "uio4"})

    def spi_to_uart():
        spi = spi_mod.Spi(link, period=6)
        spi.write(b"\x00")
        spi.end()
        uart_mod.Uart(link)
        link.check()
        link.halt()
        link.put([1, 0x5500])  # must not become UART commands after reload

    await cocotb_call(spi_to_uart)
    rx = ls.attach(UartRx("uio4", 115200))

    def new_uart():
        uart_mod.Uart(link, tx=4).write(b"\x81")

    await cocotb_call(new_uart)
    await ls.run(15000)
    assert rx.bytes == [0x81] and not rx.errors, (rx.bytes, rx.errors)

    tx = ls.attach(UartTx("uio1", 115200))
    tx.send(b"\x81\x82\x83")
    assert await cocotb_call(lambda: link.get(3, timeout_us=2000)) == [0x81, 0x82, 0x83]
    await cocotb_call(lambda: uart_mod.Uart(link, tx=4))
    tx.send(b"\x99")
    assert await cocotb_call(lambda: link.get(1, timeout_us=2000)) == [0x99]

    await cocotb_call(lambda: PulseTrigger(link, kind="edge"))
    await ls.run(1000)
    trig = await cocotb_call(lambda: PulseTrigger(link, kind="uart", match=0x42, delay=6000))
    await ls.run(1000)
    tx.send(b"\x42")
    assert await cocotb_call(lambda: trig.wait(1, timeout_us=1000)) == 1
    assert not ls.board.contention


@cocotb.test()
async def test_uc5_swd_attach(dut):
    """Use case 5: JTAG-to-SWD, DPIDR, debug power-up, MEM-AP IDR and ROM
    table, with WAIT responses along the way."""
    ls, link = await setup(dut, pullups={"uio0"})
    tgt = ls.attach(SwdTarget("uio1", "uio0", wait_count=2, pwrup_delay=100))

    def host():
        p = swd_mod.Swd(link, swdio=0, swclk=1, period=10)
        dp = p.connect(from_jtag=True)
        p.power_up()
        return dp, p.rom_table(), p.waits

    dp, rom, waits = await cocotb_call(host)
    assert dp["dpidr"] == 0x2BA01477 and dp["designer"] == "Arm", dp
    assert rom["idr"] == 0x24770011 and rom["base"] == 0xE00FF000 and rom["rom_cid_ok"], rom
    assert [e["address"] for e in rom["entries"]] == [0xE000E000, 0xE0001000], rom
    assert waits == 2
    assert not tgt.violations and not ls.board.contention
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_uc4_jtag_chain_and_pin_finder(dut):
    """Use case 4: IDCODE chain scan with JEP106 decode, BYPASS chain
    length, then identify an unlabelled header's pins."""
    ls, link = await setup(dut, pullups={"uio2", "uio7"})
    arm, xil = 0x4BA00477, 0x0362D093
    chain = ls.attach(JtagChain("uio0", "uio1", "uio2", "uio3", [Tap(4, arm), Tap(5, None), Tap(6, xil)]))
    # a second board on other pins, header unlabelled: TCK=uio5 TMS=uio6 TDI=uio7 TDO=uio4
    ls.attach(JtagChain("uio5", "uio6", "uio7", "uio4", [Tap(4, arm)]))

    def host():
        j = jtag_mod.Jtag(link, tck=0, tms=1, tdi=2, tdo=3, period=10)
        ids = j.idcodes()
        n = j.chain_length()
        found = jtag_mod.find_pins(link, candidates=(4, 5, 6, 7), period=10)
        link.check()
        return ids, n, found

    ids, n, found = await cocotb_call(host)
    assert ids == [xil, None, arm], ids
    assert n == 3
    assert found == {"tck": 5, "tms": 6, "tdi": 7, "tdo": 4, "idcode": arm}, found
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_uc8_logic_analyser(dut):
    """Use case 8: three channels captured into SRAM with cycle-exact
    timestamps, a trigger, frequency and duty measurement, VCD export."""
    import io
    ls, link = await setup(dut)
    ls.attach(Square("uio0", high=150, low=350, start=0))
    ls.attach(Square("uio1", high=80, low=80, start=5))
    ls.attach(Pulses("uio2", [110000, 110020, 140000, 140007]))
    ls.attach(Pulses("uio3", [100000], level=1))  # trigger: falls at 100000, after arming

    def host():
        la = LogicAnalyser(link, pins=(0, 1, 2), names=["a", "b", "c"], trigger=(3, "fall"), n=120)
        cap = la.capture(timeout_us=3000)
        vcd = io.StringIO()
        cap.to_vcd(vcd)
        return cap, vcd.getvalue()

    cap, vcd = await cocotb_call(host)
    a, b = cap.edges[0], cap.edges[1]
    assert not cap.overruns
    assert all(y[0] - x[0] == (150 if x[1] else 350) for x, y in zip(a, a[1:])), a[:6]
    assert all(y[0] - x[0] == 80 for x, y in zip(b, b[1:])), b[:6]
    assert [y[0] - x[0] for x, y in zip(cap.edges[2], cap.edges[2][1:])] == [20, 29980, 7]
    m = cap.measure(0)
    assert abs(m["frequency_hz"] - 100000) < 1 and abs(m["duty"] - 0.3) < 0.01
    assert "$var wire 1 ! a $end" in vcd
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


class EdgeLog:
    def __init__(self, net):
        self.net, self.prev, self.edges = net, None, []

    def step(self, cycle, nets):
        v = nets.get(self.net, 0)
        if self.prev is not None and v != self.prev:
            self.edges.append((cycle, v))
        self.prev = v
        return {}


@cocotb.test()
async def test_uc9_triggered_pulse(dut):
    """Use case 9: a pulse at an exact delay after the start bit of a
    matching UART byte; the offset is the same for every trigger."""
    ls, link = await setup(dut, pullups={"uio1"})
    tx = ls.attach(UartTx("uio1", 1000000))
    tlog = ls.attach(EdgeLog("uio1"))
    out = ls.attach(EdgeLog("uio0"))

    def host():
        t = PulseTrigger(link, out=0, trig=1, delay=1500, width=40, kind="uart", baud=1000000, match=0x5A)
        link.t.sleep_us(20)
        tx.send(b"\x00\x5a\x33\x5a", gap_bits=1)
        return t.wait(2, timeout_us=400)

    assert await cocotb_call(host) == 2
    await ls.run(2000)
    falls = [c for c, v in tlog.edges if v == 0]
    starts = [falls[0]]  # frame starts: the first fall after each 11-bit frame
    for c in falls:
        if c >= starts[-1] + 10 * 50:
            starts.append(c)
    rises = [c for c, v in out.edges if v == 1]
    offs = [r - starts[i] for r, i in zip(rises, (1, 3))]
    # delay + 4, every time: CAP = edge + 2 (input synchroniser), the pulse
    # slot begins at CAP + delay exactly, and the pin shows it 2 cycles later
    # (pad output register, board net update). The first draft of this test
    # predicted 1505 before it ever ran; 1504 is what model and RTL both give,
    # before and after the lane 3 cut.
    assert offs == [1504, 1504], (offs, starts, rises)
    assert [b - a for (a, va), (b, vb) in zip(out.edges, out.edges[1:]) if va == 1] == [40, 40]
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_uc7_i2c_eeprom_target(dut):
    """Use case 7: the chip answers as a 24C02 in fast mode. An MCU model page
    writes (with roll-over), random reads, and addresses a missing device."""
    from tempo.models.i2c import I2cBusMonitor, I2cMaster
    from tempo_host.target import Eeprom24c02Target
    ls, link = await setup(dut, pullups={"uio4", "uio5"})
    mon = ls.attach(I2cBusMonitor("uio5", "uio4", mode="fast"))
    mcu = ls.attach(I2cMaster("uio5", "uio4", half=66))  # 380 kHz, SCL low >= tLOW 1.3 us

    def host():
        ee = Eeprom24c02Target(link, sda=4, scl=5, addr=0x50, contents=bytes(range(256)))
        link.t.sleep_us(10)
        mcu.run([("start",), ("write", 0xA0), ("write", 0x06)] + [("write", 0xB0 + i) for i in range(10)]
                + [("stop",), ("start",), ("write", 0xA0), ("write", 0x06), ("start",), ("write", 0xA1),
                   ("read", True), ("read", True), ("read", True), ("read", False), ("stop",),
                   ("start",), ("write", 0xA2), ("stop",)])
        while mcu.busy:
            link.t.sleep_us(50)
        link.t.sleep_us(40)
        return ee.contents(), ee.writes()

    contents, writes = await cocotb_call(host)
    assert mcu.acks == [0] * 12 + [0, 0, 0, 1], mcu.acks
    assert mcu.reads == [0xB8, 0xB9, 0x08, 0x09], [hex(x) for x in mcu.reads]
    assert contents[0:8] == bytes(range(0xB2, 0xBA)) and contents[8:] == bytes(range(8, 256))
    assert writes == [(6, 10)]
    assert not mon.violations, mon.violations[:3]
    assert not ls.board.contention
    await cocotb_call(link.check)
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_uc6_i2c_monitor(dut):
    """Use case 6: passive I2C decode of a 400 kHz bus the chip never drives."""
    from tempo.models.i2c import I2cMaster
    from tempo_host.monitor import I2cMonitor
    ls, link = await setup(dut, pullups={"uio4", "uio5"})
    tgt = ls.attach(I2cTarget("uio5", "uio4", 0x50, regs=bytearray(range(256)), mode="fast"))
    m = ls.attach(I2cMaster("uio5", "uio4", half=63))

    def host():
        mon = I2cMonitor(link, sda=4, scl=5)
        link.t.sleep_us(4)
        m.run([("start",), ("write", 0xA0), ("write", 0x10), ("write", 0x77),
               ("start",), ("write", 0xA1), ("read", True), ("read", False), ("stop",),
               ("start",), ("write", 0x3C << 1), ("stop",)])
        ev = []
        while m.busy:
            ev += mon.poll()
            link.t.sleep_us(10)
        link.t.sleep_us(40)
        ev += mon.poll()
        return I2cMonitor.transactions(ev), mon.lost

    tx, lost = await cocotb_call(host)
    assert [(t["addr"], t["read"], t["ack"]) for t in tx] == [(0x50, 0, True), (0x50, 1, True), (0x3C, 0, False)], tx
    assert tx[0]["data"] == [(0x10, True), (0x77, True)]
    assert tx[1]["data"] == [(0x11, True), (0x12, False)]
    assert tx[1]["time"] > tx[0]["time"] and not lost
    assert tgt.regs[0x10] == 0x77
    assert not ls.board.contention
    await cocotb_call(link.check)
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_uc6_spi_monitor(dut):
    """Use case 6: an MCU model reads its flash at 3.1 MHz SCK; the chip
    records both directions of every byte without driving the bus."""
    from tempo.models.spi import SpiMaster
    from tempo_host.monitor import SpiMonitor
    ls, link = await setup(dut)
    mem = bytearray(range(256)) * 16
    ls.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=0, mem=mem, size=4096))
    mcu = ls.attach(SpiMaster("uio2", "uio0", "uio1", "uio3", mode=0, half=8))

    def host():
        mon = SpiMonitor(link, mosi=0, miso=1, sck=2, cs=3, mode=0)
        link.t.sleep_us(4)
        mcu.transfer(b"\x9f\x00\x00\x00")
        mcu.transfer(b"\x03\x00\x01\x20" + b"\x00" * 6)
        got = []
        while mcu.busy:
            got += mon.poll()
            link.t.sleep_us(8)
        link.t.sleep_us(60)
        got += mon.poll()
        return got, mon.lost

    got, lost = await cocotb_call(host)
    assert [bytes(t["mosi"][:4]) for t in got] == [b"\x9f\x00\x00\x00", b"\x03\x00\x01\x20"], got
    assert bytes(got[0]["miso"][1:4]) == b"\xef\x40\x18"
    assert bytes(got[1]["miso"][4:]) == bytes(mem[0x120:0x126])
    assert [t["count"] for t in got] == [4, 10] and not lost
    assert not any(t["flags"] for t in got)
    await cocotb_call(link.check)
    dut._log.info(f"lockstep: {ls.cycles} cycles, {ls.commits} commits matched")


@cocotb.test()
async def test_spi_monitor_preserves_transactions_during_startup(dut):
    from tempo.models.spi import SpiMaster
    from tempo_host.monitor import SpiMonitor
    ls, link = await setup(dut)
    mem = bytearray((i * 37 + 5) & 0xFF for i in range(4096))
    ls.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mem=mem, size=4096))
    mcu = ls.attach(SpiMaster("uio2", "uio0", "uio1", "uio3", half=8))

    def host():
        for k in range(150):
            mcu.transfer(bytes([3, 0, 1, k]) + bytes(6))
        mon = SpiMonitor(link)
        assert mcu.busy
        got = []
        while mcu.busy:
            got += mon.poll()
            link.t.sleep_us(4)
        for _ in range(20):
            link.t.sleep_us(10)
            got += mon.poll()
        good = [t for t in got if t["count"] == 10 and bytes(t["mosi"][:3]) == b"\x03\x00\x01"
                and bytes(t["miso"][4:]) == bytes(mem[0x100 + t["mosi"][3]:][:6])]
        assert all(t in good or t["flags"] for t in got), got
        assert good and mon.lost
        for _ in range(150):
            link.t.sleep_us(20)
            mon.poll()
        mcu.transfer(bytes([3, 0, 1, 200]) + bytes(6))
        last = []
        for _ in range(40):
            link.t.sleep_us(10)
            last += mon.poll()
        return last

    last = await cocotb_call(host)
    assert [(bytes(t["mosi"]), bytes(t["miso"][4:]), t["flags"]) for t in last] == [
        (bytes([3, 0, 1, 200]) + bytes(6), bytes(mem[0x100 + 200:][:6]), 0)], last
    assert not ls.board.contention
