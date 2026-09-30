"""Host-cost simulation must advance the live chip, not just a stopwatch."""

import pytest

from tempo.harness import Harness
from tempo.models.uart import UartTx
from tempo.transport import model_transport
from tempo_host.link import Link
from tempo_host.uart import Uart


@pytest.mark.parametrize("clock_hz,expected", [(50000000, 250), (40000000, 200)])
def test_per_call_cost_runs_live_board_at_actual_clock(clock_hz, expected):
    def read(cost):
        h = Harness(None, boot="host", clock_hz=clock_hz)
        assert h.boot_done()
        h.run(1000)  # the host CS gap has already expired in both cases
        t = model_transport(h, call_overhead_us=cost)
        link = Link(t, clock_hz=clock_hz)
        link.write_imem(20, [0xA53C, 0x96E1])
        h.run(1000)
        start = h.board.cycle
        got = link.read_imem(20, 2)
        return got, h.board.cycle - start, t

    fast, fast_cycles, _ = read(0)
    slow, slow_cycles, t = read(5)
    assert fast == slow == [0xA53C, 0x96E1]
    assert slow_cycles - fast_cycles == expected
    assert t.xfers == 2


def test_uart_keeps_receiving_during_host_call_cost():
    h = Harness(None, boot="host", pullups={"uio1"})
    tx = h.attach(UartTx("uio1", 1000000))
    t = model_transport(h, call_overhead_us=100)
    u = Uart(Link(t), baud=1000000)
    data = bytes(range(32))
    tx.send(data)
    # Each host call stalls for ten character times. The live firmware must
    # receive into SRAM while the host is busy, then return the exact bytes.
    assert u.read(len(data), timeout_us=20000) == data
    assert not h.board.contention


def test_reject_negative_host_call_cost():
    h = Harness(None, boot="host")
    with pytest.raises(ValueError):
        model_transport(h, call_overhead_us=-1)
