"""Host library bus monitors against the chip model (no RTL)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.i2c import I2cMaster, I2cTarget  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host.monitor import I2cMonitor  # noqa: E402


def test_i2c_monitor_decodes_live_bus():
    h = Harness(None, boot="host", pullups={"uio4", "uio5"})
    tgt = h.attach(I2cTarget("uio5", "uio4", 0x50, regs=bytearray(range(256)), mode="fast"))
    m = h.attach(I2cMaster("uio5", "uio4", half=63))  # ~400 kHz
    link = Link(model_transport(h))
    mon = I2cMonitor(link, sda=4, scl=5)
    h.run(200)
    m.run([("start",), ("write", 0xA0), ("write", 0x10), ("write", 0x77),
           ("start",), ("write", 0xA1), ("read", True), ("read", False), ("stop",),
           ("start",), ("write", 0x3C << 1), ("stop",)])
    ev = []
    while m.busy:
        ev += mon.poll()
        h.run(500)
    h.run(2000)
    ev += mon.poll()
    tx = I2cMonitor.transactions(ev)
    assert [(t["addr"], t["read"], t["ack"]) for t in tx] == [(0x50, 0, True), (0x50, 1, True), (0x3C, 0, False)], tx
    assert tx[0]["data"] == [(0x10, True), (0x77, True)]
    assert tx[1]["data"] == [(0x11, True), (0x12, False)]
    assert tx[1]["time"] > tx[0]["time"] and not mon.lost
    assert tgt.regs[0x10] == 0x77
    assert not h.board.contention


def test_spi_monitor_sees_mcu_reading_its_flash():
    from tempo.models.spi import SpiMaster, SpiNorFlash
    from tempo_host.monitor import SpiMonitor
    h = Harness(None, boot="host")
    mem = bytearray(range(256)) * 16
    h.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=0, mem=mem, size=4096))
    mcu = h.attach(SpiMaster("uio2", "uio0", "uio1", "uio3", mode=0, half=20))  # 1.25 MHz, the measured limit
    link = Link(model_transport(h))
    mon = SpiMonitor(link, mosi=0, miso=1, sck=2, cs=3, mode=0)
    h.run(200)
    mcu.transfer(b"\x9f\x00\x00\x00")
    mcu.transfer(b"\x03\x00\x01\x20" + b"\x00" * 6)
    got = []
    while mcu.busy:
        got += mon.poll()
        h.run(400)
    h.run(3000)
    got += mon.poll()
    assert [bytes(t["mosi"][:4]) for t in got] == [b"\x9f\x00\x00\x00", b"\x03\x00\x01\x20"], got
    assert bytes(got[0]["miso"][1:4]) == b"\xef\x40\x18"
    assert bytes(got[1]["miso"][4:]) == bytes(mem[0x120:0x126])
    assert [t["count"] for t in got] == [4, 10] and not mon.lost
    assert got[1]["time"] > got[0]["time"]
    link.check()  # no UNDER (missed SCK edges) or OVERRUN
