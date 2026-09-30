"""Resident protocol concurrency and firmware-defined line-code demos."""

import pytest

from tempo.demo_checks import (PAYLOAD, LoggedI2cTarget, LoggedUart, assert_overlap,
                               observe_stretch_holds, prepare)
from tempo.harness import Harness
from tempo.models.i2c import I2cBusMonitor
from tempo.models.linecode import ManchesterFrame, ManchesterStream
from tempo.transport import model_transport
from tempo_host.link import Link


@pytest.mark.parametrize("cost", [0, 20, 100])
def test_three_protocols_run_in_one_image_under_memory_and_host_load(cost):
    h = Harness(None, boot="host", pullups={"uio4", "uio5"})
    target = h.attach(LoggedI2cTarget("uio5", "uio4", 0x68, regs=bytearray(range(256)),
                               mode="fast", stretch_cycles=300))
    monitor = h.attach(I2cBusMonitor("uio5", "uio4", mode="fast"))
    holds = observe_stretch_holds(h.board, "uio5")
    link = Link(model_transport(h, call_overhead_us=cost))
    im = prepare(link, "demo_multibus")
    uart = h.attach(LoggedUart("uio0", 1000000))
    coded = h.attach(ManchesterStream("uio1", PAYLOAD))
    link.set_pc(0, im["consts"]["uart_run"])
    link.set_pc(1, im["consts"]["mc_run"])
    link.control(run_set=3)
    uart_before = len(uart.bytes)
    start = h.board.cycle
    link.put([1 << 8])  # START
    for byte in [0x68 << 1, 0x10, 0xAB, 0xCD]:
        link.put([3 << 8 | byte])
        assert link.get(1) == [0]
    link.put([2 << 8])  # STOP
    h.run(5000)
    assert target.regs[0x10:0x12] == b"\xab\xcd"
    assert not target.violations and not monitor.violations
    assert len(uart.bytes) > uart_before + 10
    assert bytes(uart.bytes) == (b"\x55\xaa" * ((len(uart.bytes) + 1) // 2))[:len(uart.bytes)]
    assert not uart.errors
    assert all(b - a == 500 for a, b in zip(uart.starts, uart.starts[1:]))
    assert coded.frames and all(f.checked_cycles == 1600 for f in coded.frames)
    assert not coded.errors
    assert_overlap(uart, coded, target, holds)
    assert start < h.board.cycle
    assert link.status().run & 12 == 12  # I2C service and memory workload remain live
    link.halt(4)  # inspect the background thread's memory progress after stopping it
    count = link.read_reg(2, 2)
    scratch = link.read_reg(2, 3)
    assert count > 10 and (count - scratch) & 0xFFFF in (0, 1)
    assert link.read_reg(2, 4) == 0xA596
    link.check()
    assert not h.board.contention


@pytest.mark.parametrize("start_phase", [None, 64000, 65000])
def test_manchester_symbols_alone_are_firmware_settings(start_phase):
    h = Harness(None, boot="host")
    link = Link(model_transport(h))
    im = prepare(link, "manchester")
    coded = h.attach(ManchesterFrame("uio1", PAYLOAD))
    link.set_pc(1, im["consts"]["mc_run"])
    if start_phase is not None:
        h.run((start_phase - h.board.cycle) % 65536)
    link.control(run_set=2)
    h.run(4000)
    assert coded.done and coded.checked_cycles == 1600 and not coded.errors
    assert coded.checked_idle_cycles == 150
    assert not link.status().run & 2
    # The registered pad plus the device's prior-cycle input add two cycles
    # to the queued internal start deadline. Compare modulo the NOW16 clock.
    deadline = link.read_reg(1, 6)
    assert (coded.start_cycle - deadline) & 0xFFFF == 2
    if start_phase == 65000:
        assert ((deadline - 1000) & 0xFFFF) > 64535 and deadline < 1000
    elif start_phase == 64000:
        assert (coded.start_cycle & 0xFFFF) > 65536 - 1600
        assert coded.start_cycle // 65536 != (coded.start_cycle + 1599) // 65536
    link.check()


def test_wire_checker_rejects_a_bad_midpoint_and_reversed_bit():
    checker = ManchesterFrame("wire", PAYLOAD)
    bits = [(byte >> (7 - i)) & 1 for byte in PAYLOAD for i in range(8)]
    for cycle in range(1601):
        # After the last payload bit the real firmware drives low idle.
        # Reusing the last bit at cycle 1600 would create an unintended tail.
        value = bits[cycle // 50] ^ int(cycle % 50 >= 25) if cycle < 1600 else 0
        if cycle == 27 or 100 <= cycle < 150:
            value ^= 1
        checker.step(cycle, {"wire": value})
    assert checker.done and [e[0] for e in checker.errors] == [27] + list(range(100, 150))


def test_wire_checker_rejects_an_extra_frame_in_the_idle_window():
    checker = ManchesterFrame("wire", PAYLOAD)
    bits = [(byte >> (7 - i)) & 1 for byte in PAYLOAD for i in range(8)]
    for cycle in range(1750):
        value = bits[cycle // 50] ^ int(cycle % 50 >= 25) if cycle < 1600 else 0
        if cycle == 1700:
            value = 1
        checker.step(cycle, {"wire": value})
    assert checker.checked_idle_cycles == 150
    assert checker.errors == [(1700, 34, 0, 1)]


def test_wire_checker_rejects_a_shifted_midpoint_in_every_bit():
    checker = ManchesterFrame("wire", PAYLOAD)
    bits = [(byte >> (7 - i)) & 1 for byte in PAYLOAD for i in range(8)]
    for cycle in range(1750):
        value = bits[cycle // 50] ^ int(cycle % 50 >= 26) if cycle < 1600 else 0
        checker.step(cycle, {"wire": value})
    assert [e[0] for e in checker.errors] == list(range(25, 1600, 50))


def test_overlap_requires_controller_release_held_low_by_target():
    from types import SimpleNamespace
    uart = SimpleNamespace(starts=[20])
    coded = SimpleNamespace(frames=[SimpleNamespace(start_cycle=20)])
    target = SimpleNamespace(events=[SimpleNamespace(kind="start", cycle=0),
                                      SimpleNamespace(kind="stop", cycle=200)],
                             stretches=[(0, 200)])
    for holds in [[], [(0, 10)]]:
        with pytest.raises(AssertionError, match="no three-protocol overlap"):
            assert_overlap(uart, coded, target, holds)
    assert assert_overlap(uart, coded, target, [(25, 50)]) == [(25, 50)]
