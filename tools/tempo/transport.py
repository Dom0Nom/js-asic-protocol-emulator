"""Transports that run the host library (host/tempo) against simulation.

`SimTransport` turns each host transaction into a HostDriver transaction on
the board and runs the simulation until it completes. The same class serves
the Python model (synchronous `run`/`run_until`) and the cocotb lockstep
testbench (the host code runs in a `cocotb.bridge` thread and the runners are
`cocotb.resume` wrappers around the async lockstep methods).

Time on the host side is simulated time: `ticks_us` is the board cycle count
converted at the board clock, and `sleep_us` runs the simulation.
An optional call_overhead_us runs the live board before every SPI call,
representing host preparation/scheduling cost. This is an assumed cost for
sensitivity analysis, not a measurement of RP2350 or MicroPython latency.
"""

import os
import sys

from .clock import CLOCK_HZ

HOST_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "host")
if HOST_DIR not in sys.path:
    sys.path.insert(0, HOST_DIR)


class SimTransport:
    def __init__(self, driver, run, run_until, board, clock_hz=CLOCK_HZ, record=None,
                 call_overhead_us=0):
        if call_overhead_us < 0:
            raise ValueError("host call overhead must be nonnegative")
        self.drv = driver
        self._run = run
        self._run_until = run_until
        self.board = board
        self.clock_hz = clock_hz
        self.record = record  # list of (request, response) for replay checks
        self.xfers = 0
        self.bytes = 0
        self.call_overhead_us = call_overhead_us
        self.call_overhead_cycles = int(round(call_overhead_us * clock_hz / 1e6))

    def xfer(self, data):
        if self.call_overhead_cycles:
            self._run(self.call_overhead_cycles)
        t = self.drv.xfer(data)
        limit = 64 * self.drv.half * (len(data) + 8) + 1000
        ok = self._run_until(lambda: t.done, limit)
        if not ok:
            raise RuntimeError("host transaction did not complete")
        rx = bytes(t.rx)
        self.xfers += 1
        self.bytes += len(data)
        if self.record is not None:
            self.record.append((bytes(data), rx))
        return rx

    def sleep_us(self, us):
        self._run(max(1, int(us * self.clock_hz / 1e6)))

    def ticks_us(self):
        return int(self.board.cycle * 1e6 / self.clock_hz)


def model_transport(h, record=None, call_overhead_us=0):
    """Transport over a tools/tempo Harness booted with boot="host"."""
    return SimTransport(h.host, h.run, h.run_until, h.board, h.board.clock_hz, record,
                        call_overhead_us)


def cocotb_transport(ls, record=None, call_overhead_us=0):
    """Transport over a test/lockstep.py Lockstep. Call host code through
    `await cocotb_call(fn, *args)`."""
    from cocotb.task import resume
    return SimTransport(ls.h.host, resume(ls.run), resume(ls.run_until), ls.board,
                        ls.board.clock_hz, record, call_overhead_us)


async def cocotb_call(fn, *args, **kw):
    """Run blocking host-library code against the lockstep testbench."""
    from cocotb.task import bridge
    return await bridge(fn)(*args, **kw)
