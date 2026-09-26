"""Host library I2C target emulation against the chip model (no RTL)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.i2c import I2cBusMonitor, I2cMaster  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host.target import Eeprom24c02Target  # noqa: E402


def run(h, mcu):
    while mcu.busy:
        h.run(500)


def test_mcu_writes_and_random_reads_emulated_eeprom():
    h = Harness(None, boot="host", pullups={"uio4", "uio5"})
    mon = h.attach(I2cBusMonitor("uio5", "uio4", mode="standard"))
    mcu = h.attach(I2cMaster("uio5", "uio4", half=250))  # 100 kHz
    link = Link(model_transport(h))
    ee = Eeprom24c02Target(link, sda=4, scl=5, addr=0x50, contents=bytes(range(256)))
    h.run(500)
    # page write of 10 bytes at 0x06: rolls over inside the page 0x00-0x07
    mcu.run([("start",), ("write", 0xA0), ("write", 0x06)] + [("write", 0xB0 + i) for i in range(10)] + [("stop",)])
    run(h, mcu)
    assert mcu.acks == [0] * 12, mcu.acks
    # random read: word address, repeated START, 4 bytes
    mcu.run([("start",), ("write", 0xA0), ("write", 0x06), ("start",), ("write", 0xA1),
             ("read", True), ("read", True), ("read", True), ("read", False), ("stop",)])
    run(h, mcu)
    h.run(2000)
    # writes roll over inside the page, sequential reads run on across it
    # (docs/specs.md section 2): 0x06, 0x07, then 0x08 and 0x09 unchanged
    assert mcu.reads == [0xB8, 0xB9, 0x08, 0x09], [hex(x) for x in mcu.reads]
    # another address is not answered
    mcu.run([("start",), ("write", 0xA2), ("stop",)])
    run(h, mcu)
    assert mcu.acks[-1] == 1
    c = ee.contents()
    assert c[0:8] == bytes(range(0xB2, 0xBA))  # 0x06, 0x07, then 0x00..0x07 and c[8] == 8
    assert ee.writes() == [(6, 10)]
    assert not mon.violations, mon.violations[:3]
    assert not h.board.contention
    link.check()


def test_stretching_target_slows_the_controller():
    h = Harness(None, boot="host", pullups={"uio4", "uio5"})
    mcu = h.attach(I2cMaster("uio5", "uio4", half=63))  # 400 kHz
    link = Link(model_transport(h))
    Eeprom24c02Target(link, sda=4, scl=5, contents=bytes(range(256)), stretch=2000)
    h.run(500)
    t0 = h.board.cycle
    mcu.run([("start",), ("write", 0xA0), ("write", 0x20), ("start",), ("write", 0xA1), ("read", False), ("stop",)])
    run(h, mcu)
    assert mcu.reads == [0x20]
    assert h.board.cycle - t0 > 3 * 2000  # three ACKs, each stretched 2000 cycles
