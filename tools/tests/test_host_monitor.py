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
