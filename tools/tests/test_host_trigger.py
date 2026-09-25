"""Host library triggered pulse against the chip model (no RTL)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.siggen import Pulses  # noqa: E402
from tempo.models.uart import UartTx  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link, TempoError  # noqa: E402
from tempo_host.trigger import PulseTrigger  # noqa: E402


class EdgeLog:
    """Records every edge of a net: (cycle, level)."""

    def __init__(self, net):
        self.net, self.prev, self.edges = net, None, []

    def step(self, cycle, nets):
        v = nets.get(self.net, 0)
        if self.prev is not None and v != self.prev:
            self.edges.append((cycle, v))
        self.prev = v
        return {}


def test_uart_byte_trigger_exact_delay():
    h = Harness(None, boot="host", pullups={"uio1"})
    tx = h.attach(UartTx("uio1", 115200))
    tlog = h.attach(EdgeLog("uio1"))
    out = h.attach(EdgeLog("uio0"))
    link = Link(model_transport(h))
    trig = PulseTrigger(link, out=0, trig=1, delay=6000, width=100, kind="uart", match=0x42)
    h.run(1000)  # let the firmware arm before the traffic starts
    tx.send(b"\x41\x42\x43\x42")
    assert trig.wait(2, timeout_us=5000) == 2
    h.run(8000)
    starts = [c for c, v in tlog.edges]
    # start bits of the two 0x42 bytes (bytes 1 and 3; 10 bits of 434 cycles each)
    frames = [c for c, v in tlog.edges if v == 0]
    b1 = min(c for c in frames if c > frames[0] + 9 * 434)
    b3 = min(c for c in frames if c > b1 + 2 * 9 * 434)
    pulses = [c for c, v in out.edges if v == 1]
    widths = [b - a for (a, va), (b, vb) in zip(out.edges, out.edges[1:]) if va == 1]
    offs = [p - b for p, b in zip(pulses, (b1, b3))]
    assert offs[0] == offs[1] and 6000 <= offs[0] <= 6006, offs  # fixed pipeline offset
    assert widths == [100, 100]


def test_edge_count_trigger_and_late_flag():
    h = Harness(None, boot="host")
    edges_at = [40000 + 1000 * k for k in range(9)]
    trig_in = h.attach(Pulses("uio1", edges_at))
    out = h.attach(EdgeLog("uio0"))
    link = Link(model_transport(h))
    trig = PulseTrigger(link, out=0, trig=1, delay=300, width=20, kind="edge", count=3)
    assert trig.wait(3, timeout_us=5000) == 3
    h.run(2000)
    rises = [c for c, v in out.edges if v == 1]
    # every third edge (3rd, 6th, 9th) plus the same fixed offset
    offs = [r - e for r, e in zip(rises, edges_at[2::3])]
    assert len(set(offs)) == 1 and 300 <= offs[0] <= 306, offs
    # a delay the firmware cannot meet is flagged, not silently shifted
    h2 = Harness(None, boot="host")
    h2.attach(Pulses("uio1", [40000, 40001]))
    link2 = Link(model_transport(h2))
    t2 = PulseTrigger(link2, out=0, trig=1, delay=2, width=5, kind="edge", count=1)
    with pytest.raises(TempoError, match="lane 0 LATE"):
        t2.wait(1, timeout_us=2000)
