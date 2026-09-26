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
    mcu = h.attach(SpiMaster("uio2", "uio0", "uio1", "uio3", mode=0, half=8))  # 3.1 MHz, 20-cycle CS gaps: the measured limit
    link = Link(model_transport(h))
    mon = SpiMonitor(link, mosi=0, miso=1, sck=2, cs=3, mode=0)
    h.run(200)
    mcu.transfer(b"\x9f\x00\x00\x00")
    mcu.transfer(b"\x03\x00\x01\x20" + b"\x00" * 6)
    got = []
    while mcu.busy:
        got += mon.poll()
        h.run(400)
    for _ in range(20):
        h.run(1000)
        got += mon.poll()
    assert [bytes(t["mosi"][:4]) for t in got] == [b"\x9f\x00\x00\x00", b"\x03\x00\x01\x20"], got
    assert bytes(got[0]["miso"][1:4]) == b"\xef\x40\x18"
    assert bytes(got[1]["miso"][4:]) == bytes(mem[0x120:0x126])
    assert [t["count"] for t in got] == [4, 10] and not mon.lost
    assert got[1]["time"] > got[0]["time"]
    link.check()  # no UNDER (missed SCK edges) or OVERRUN


def _spi_run(half, n, ntx, keep=None):
    from tempo.models.spi import SpiMaster, SpiNorFlash
    from tempo_host.monitor import SpiMonitor
    h = Harness(None, boot="host")
    mem = bytearray((i * 37 + 5) & 0xFF for i in range(4096))
    h.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=0, mem=mem, size=4096))
    mcu = h.attach(SpiMaster("uio2", "uio0", "uio1", "uio3", mode=0, half=half))
    link = Link(model_transport(h))
    mon = SpiMonitor(link, keep=keep)
    h.run(300)
    for _ in range(ntx):
        mcu.transfer(b"\x03\x00\x01\x00" + b"\x00" * n)
    got = []
    while mcu.busy:
        got += mon.poll()
        h.run(200)
    for _ in range(200):
        h.run(500)
        got += mon.poll()
        if len(got) >= ntx:
            break
    return got, mem, link


def test_spi_monitor_limits():
    # one long transaction: 5 MHz keeps every byte (148 cycles per 16-bit word)
    got, mem, link = _spi_run(5, 256, 1)
    assert len(got) == 1 and got[0]["count"] == 260 and bytes(got[0]["miso"][4:]) == bytes(mem[0x100:0x200])
    link.check()
    # 6.25 MHz: the firmware falls behind, and the chip says so (UNDER). The
    # firmware reads and clears both lanes' STAT at CS rise and forwards the
    # flags in the transaction's end record (bug 32), so that is where it shows.
    got, mem, link = _spi_run(4, 256, 1)
    assert len(got) == 1 and got[0]["flags"] & 2 and got[0]["count"] < 260


def test_spi_monitor_armed_mid_traffic():
    # Bug 32: traffic already running while the firmware arms. A lane that
    # missed edges used to leave MISO paired one word off and then block the
    # monitor for good, with no flag. The traffic (20-cycle CS gaps) also
    # outruns the host link, so the chip's rings fill and events are
    # dropped. Every transaction reported must be intact or flagged, and the
    # monitor must keep going.
    from tempo.models.spi import SpiMaster, SpiNorFlash
    from tempo_host.monitor import SpiMonitor
    h = Harness(None, boot="host")
    mem = bytearray((i * 37 + 5) & 0xFF for i in range(4096))
    h.attach(SpiNorFlash("uio2", "uio0", "uio1", "uio3", mode=0, mem=mem, size=4096))
    mcu = h.attach(SpiMaster("uio2", "uio0", "uio1", "uio3", mode=0, half=8))
    link = Link(model_transport(h))
    for k in range(150):  # loading and arming take about 65k cycles
        mcu.transfer(bytes([0x03, 0x00, 0x01, k]) + b"\x00" * 6)
    mon = SpiMonitor(link)
    assert mcu.busy  # arming finished while the MCU was still sending
    got = []
    while mcu.busy:
        got += mon.poll()
        h.run(200)
    for _ in range(20):
        h.run(500)
        got += mon.poll()
    good = [t for t in got if t["count"] == 10 and bytes(t["mosi"][:3]) == b"\x03\x00\x01"
            and bytes(t["miso"][4:]) == bytes(mem[0x100 + t["mosi"][3]:][:6])]
    assert all(t in good or t["flags"] for t in got), got
    assert good and mon.lost
    # once the backlog has drained, the next transaction arrives intact
    for _ in range(150):  # the ring holds up to 512 words
        h.run(1000)
        mon.poll()
    mcu.transfer(bytes([0x03, 0x00, 0x01, 200]) + b"\x00" * 6)
    last = []
    for _ in range(40):
        h.run(500)
        last += mon.poll()
    assert [(bytes(t["mosi"]), bytes(t["miso"][4:]), t["flags"]) for t in last] == [
        (bytes([3, 0, 1, 200]) + bytes(6), bytes(mem[0x100 + 200:][:6]), 0)], last


def test_spi_monitor_keep_counts_the_rest():
    got, mem, link = _spi_run(8, 64, 2, keep=6)
    assert [t["count"] for t in got] == [68, 68]
    assert [len(t["mosi"]) for t in got] == [6, 6] and bytes(got[0]["mosi"][:4]) == b"\x03\x00\x01\x00"
    link.check()
