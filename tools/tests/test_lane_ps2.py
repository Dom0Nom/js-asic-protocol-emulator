import pytest

from tempo.harness import Harness
from tempo.models.ps2 import Ps2Device, Ps2Host

CLOCKS = [25e6, 30e6]
DEV_DATA, DEV_CLK = "uio0", "uio1"
HOST_DATA, HOST_CLK = "uio2", "uio3"
PARITY_ERR, START_ERR, STOP_ERR = 1 << 8, 1 << 9, 1 << 10
BYTES = [0x00, 0xFF, 0x01, 0x03, 0xA5, 0x80, 0x7E]  # 0x01 and 0x80 have an odd count of ones, 0x03 an even one


def harness(clock_hz):
    h = Harness(f'.equ PS2_CLK_KHZ, {int(clock_hz // 1000)}\n.include "ps2.s"\n', clock_hz=clock_hz,
                pullups={DEV_DATA, DEV_CLK, HOST_DATA, HOST_CLK})
    h.run(200)
    return h


def frames(clock_hz, n, period_us=100):
    return int(n * 11 * period_us * clock_hz / 1e6)


def run(h, cycles, to_chip=()):
    """Run, feeding the 4-word host mailbox as it empties and draining replies."""
    to_chip, got = list(to_chip), []
    for _ in range(int(cycles) // 64 + 1):
        while to_chip and len(h.chip.h2t) < 4:
            h.chip.h2t.append(to_chip.pop(0))
        h.run(64)
        while h.chip.t2h:
            got.append(h.chip.t2h.popleft())
    assert not to_chip
    return got


def assert_clean(h):
    assert not h.chip.illegal
    assert not h.board.contention
    assert h.chip.lanes[1].stat == 0 and h.chip.lanes[2].stat == 0  # no UNDER, OVERRUN or LATE


@pytest.mark.parametrize("clock_hz", CLOCKS)
def test_device_sends_frames(clock_hz):
    h = harness(clock_hz)
    host = h.attach(Ps2Host(DEV_CLK, DEV_DATA, clock_hz=clock_hz))
    run(h, frames(clock_hz, len(BYTES) + 1), BYTES)
    assert host.bytes == BYTES, [hex(b) for b in host.bytes]
    assert not host.violations, host.violations[:4]
    assert_clean(h)


def test_device_bit_timing():
    """80 us bit: DATA 20 us before the falling edge, clock low 40 us."""
    clock_hz = 25e6
    h = harness(clock_hz)
    edges = []

    class Probe:
        prev = (1, 1)

        def step(self, cycle, nets):
            cur = (nets.get(DEV_CLK, 1), nets.get(DEV_DATA, 1))
            if cur != self.prev:
                edges.append((cycle, *cur))
                self.prev = cur
            return {}

    h.attach(Probe())
    run(h, frames(clock_hz, 2), [0x00])
    t0 = edges[0][0]
    assert [(t - t0, c, d) for t, c, d in edges[:5]] == [
        (0, 1, 0), (500, 0, 0), (1500, 1, 0), (2500, 0, 0), (3500, 1, 0)]
    falls = [t for (t, c, d), (_, pc, _) in zip(edges[1:], edges) if pc == 1 and c == 0]
    assert len(falls) == 11 and {b - a for a, b in zip(falls, falls[1:])} == {2000}


@pytest.mark.parametrize("clock_hz, period_us, setup_us", [
    (25e6, 100, 5), (25e6, 100, 25), (25e6, 60, 5), (25e6, 60, 25), (30e6, 100, 25), (30e6, 60, 5)])
def test_host_receives_at_legal_clock_rates(clock_hz, period_us, setup_us):
    h = harness(clock_hz)
    dev = h.attach(Ps2Device(HOST_CLK, HOST_DATA, period_us=period_us, setup_us=setup_us, gap_us=50,
                             clock_hz=clock_hz))
    ref = h.attach(Ps2Host(HOST_CLK, HOST_DATA, clock_hz=clock_hz))
    data = BYTES[:5]
    dev.send(data)
    got = run(h, frames(clock_hz, len(data) + 1, period_us + 5))
    assert got == data, [hex(w) for w in got]
    assert ref.bytes == data and not ref.violations, ref.violations[:4]
    assert_clean(h)


def test_host_flags_bad_parity_and_stop():
    clock_hz = CLOCKS[0]
    h = harness(clock_hz)
    dev = h.attach(Ps2Device(HOST_CLK, HOST_DATA, clock_hz=clock_hz))
    ref = h.attach(Ps2Host(HOST_CLK, HOST_DATA, clock_hz=clock_hz))
    dev.send([0x5A])
    dev.send([0x5A, 0x00, 0xFF], bad_parity=True)
    dev.send([0x13], bad_stop=True)
    dev.send([0xC3])
    got = run(h, frames(clock_hz, 7))
    assert got == [0x5A, PARITY_ERR | 0x5A, PARITY_ERR | 0x00, PARITY_ERR | 0xFF, STOP_ERR | 0x13, 0xC3], \
        [hex(w) for w in got]
    assert [v.kind for v in ref.violations] == ["parity"] * 3 + ["stop"]
    assert_clean(h)


def test_host_flags_start_bit_high():
    """Start bit high, data 0x00 with a correct parity bit and stop bit."""
    clock_hz = CLOCKS[0]
    h = harness(clock_hz)
    us = clock_hz / 1e6

    class Raw:
        def step(self, cycle, nets):
            t = cycle - 1000
            k, ph = divmod(t, int(80 * us))
            if t < 0 or k >= 11:
                return {}
            low = int(15 * us) <= ph < int(55 * us)
            return {HOST_CLK: 0 if low else None, HOST_DATA: 0 if 1 <= k <= 8 else None}

    h.attach(Raw())
    got = run(h, frames(clock_hz, 2))
    assert got == [START_ERR | 0x00], [hex(w) for w in got]


@pytest.mark.parametrize("clock_hz", CLOCKS)
def test_both_roles_at_once(clock_hz):
    h = harness(clock_hz)
    host = h.attach(Ps2Host(DEV_CLK, DEV_DATA, clock_hz=clock_hz))
    dev = h.attach(Ps2Device(HOST_CLK, HOST_DATA, period_us=60, setup_us=10, clock_hz=clock_hz))
    out = [0x1C, 0xF0, 0x00, 0xFF]
    inc = [0xFA, 0xAA, 0x00, 0xFF, 0x01]
    dev.send(inc)
    got = run(h, frames(clock_hz, 5), out)
    assert host.bytes == out, [hex(b) for b in host.bytes]
    assert not host.violations, host.violations[:4]
    assert got == inc, [hex(w) for w in got]
    assert_clean(h)


def test_checker_rejects_illegal_timing():
    """The checker itself: a device outside the spec is reported."""
    from tempo.board import Board
    clock_hz = 25e6
    for field, value, kind in (("period", 2600, "period"), ("low", 600, "clock_low"), ("setup", 50, "setup"),
                               ("setup", 700, "setup")):
        board = Board(clock_hz=clock_hz, pullups={"c", "d"})
        dev = board.attach(Ps2Device("c", "d", clock_hz=clock_hz))
        host = board.attach(Ps2Host("c", "d", clock_hz=clock_hz))
        setattr(dev, field, value)
        dev.send([0x55])
        for _ in range(40000):
            board.tick({})
        assert host.bytes == [0x55]
        assert kind in {v.kind for v in host.violations}, (field, value, host.violations[:3])
