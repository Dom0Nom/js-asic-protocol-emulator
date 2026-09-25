"""Host library I2C use case against the chip model (no RTL)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.i2c import Eeprom24Target, I2cTarget, StuckSda  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host.i2c import I2c, Eeprom24, Nack  # noqa: E402


def board(*devs, mode="fm"):
    h = Harness(None, boot="host", pullups={"uio4", "uio5"})
    for d in devs:
        h.attach(d)
    link = Link(model_transport(h))
    return h, I2c(link, sda=4, scl=5, mode=mode)


def test_scan_finds_targets():
    a = I2cTarget("uio5", "uio4", 0x3C, mode="fast")
    b = Eeprom24Target("uio5", "uio4", address=0x50, mode="fast")
    h, i2c = board(a, b)
    assert i2c.scan() == [0x3C, 0x50]
    assert not a.violations and not b.violations
    assert not h.board.contention


def test_register_dump_and_write():
    regs = bytearray(range(256))
    t = I2cTarget("uio5", "uio4", 0x68, regs=regs, mode="fast", stretch_cycles=30)
    h, i2c = board(t)
    assert i2c.read_regs(0x68, 0x10, 4) == bytes([0x10, 0x11, 0x12, 0x13])
    i2c.write_regs(0x68, 0x20, b"\xde\xad")
    assert regs[0x20:0x22] == b"\xde\xad"
    with pytest.raises(Nack):
        i2c.read_regs(0x69, 0, 1)
    assert not t.violations


def test_eeprom_24c02_page_write():
    e = Eeprom24Target("uio5", "uio4", size=256, page=8, abytes=1, t_wr=40e-6, mode="fast")
    h, i2c = board(e)
    ee = Eeprom24(i2c, "24c02")
    data = bytes(range(0x40, 0x40 + 20))
    ee.write(0x05, data)  # crosses two page boundaries: 3 + 8 + 8 + 1 bytes
    assert e.writes == 4
    assert ee.read(0x05, 20) == data
    assert not e.violations


def test_eeprom_24c32_two_address_bytes():
    e = Eeprom24Target("uio5", "uio4", size=4096, page=32, abytes=2, t_wr=40e-6, mode="fast")
    h, i2c = board(e)
    ee = Eeprom24(i2c, "24c32")
    ee.write(0x0FF0, b"\x01\x02\x03\x04")
    assert ee.read(0x0FF0, 4) == b"\x01\x02\x03\x04"


def test_bus_recovery():
    s = StuckSda("uio5", "uio4", release_after=5)
    h, i2c = board(s)
    assert i2c.recover() == 5


def test_ten_bit_addressing():
    t = I2cTarget("uio5", "uio4", 0x2A5, regs=bytearray(range(256)), mode="fast", addr10=True)
    h, i2c = board(t)
    i2c.transfer(0x2A5, b"\x40\x99", addr10=True)
    assert t.regs[0x40] == 0x99
    assert i2c.transfer(0x2A5, b"\x41", 2, addr10=True) == bytes([0x41, 0x42])
    with pytest.raises(Nack):
        i2c.transfer(0x1A5, b"\x00", 1, addr10=True)
    assert not t.violations


def test_fast_mode_plus_timing():
    t = I2cTarget("uio5", "uio4", 0x3C, regs=bytearray(range(256)), mode="fastplus")
    h, i2c = board(t, mode="fmp")
    assert i2c.read_regs(0x3C, 5, 2) == bytes([5, 6])
    assert not t.violations, t.violations[:3]
