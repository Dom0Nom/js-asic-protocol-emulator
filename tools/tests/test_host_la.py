"""Host library logic analyser against the chip model (no RTL)."""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.siggen import Pulses, Square  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host.la import LogicAnalyser  # noqa: E402


def deltas(edges):
    return [b[0] - a[0] for a, b in zip(edges, edges[1:])]


def test_capture_exact_intervals_markers_and_vcd():
    h = Harness(None, boot="host")
    sq = h.attach(Square("uio0", high=300, low=700, start=20000))
    far = h.attach(Pulses("uio2", []))
    link = Link(model_transport(h))
    la = LogicAnalyser(link, pins=(0, 1, 2), names=["clk", "idle", "rare"], n=150)
    # Include every rare edge after the loader's cleanup and capture arm.
    far.at = [h.board.cycle + c for c in (10000, 10050, 60000, 60013)]
    cap = la.capture(timeout_us=4000)
    assert not cap.overruns
    got0 = cap.edges[0]
    assert len(got0) > 100
    # the chip's intervals equal the generator's, to the cycle: 300 high, 700 low
    assert all(b[0] - a[0] == (300 if a[1] else 700) for a, b in zip(got0, got0[1:]))
    assert deltas(cap.edges[2]) == [50, 49950, 13]  # a 50000-cycle gap spans many markers
    m = cap.measure(0)
    assert abs(m["frequency_hz"] - 50000) < 1 and abs(m["duty"] - 0.3) < 0.01
    buf = io.StringIO()
    cap.to_vcd(buf)
    assert "$var wire 1 ! clk $end" in buf.getvalue()


def test_trigger_on_falling_edge():
    h = Harness(None, boot="host")
    trigger = h.attach(Pulses("uio3", [], level=1))
    sig = h.attach(Square("uio0", high=100, low=100, start=10000))
    link = Link(model_transport(h))
    la = LogicAnalyser(link, pins=(0,), trigger=(3, "fall"), n=20)
    # Keep the original fixture's phase: trigger on a square-wave edge,
    # leaving 100 cycles to arm before the first edge we expect to capture.
    trigger_at = ((h.board.cycle + 10000) // 200 + 1) * 200
    trigger.at = [trigger_at, trigger_at + 400]
    cap = la.capture(timeout_us=4000)
    first = cap.edges[0][0][0] + cap.start
    # the first captured edge is the first square edge after the trigger
    trig_edges = [c for c, _ in sig.edges if c > trigger_at]
    assert len(cap.edges[0]) >= 15 and abs((first - trig_edges[0]) % 65536) < 8
