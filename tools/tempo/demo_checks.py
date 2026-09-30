"""Shared model/RTL verification helpers for the composed firmware demo.

Devices observe wire cycles; loading uses the public host port.
This module is a CPython verification tool, not a MicroPython dependency.
"""

from .models.i2c import I2cTarget
from .models.uart import UartRx


PAYLOAD = b"\xa5\x96\x3c\x69"


class LoggedUart(UartRx):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.starts = []

    def _start_frame(self, cycle):
        self.starts.append(cycle)
        super()._start_frame(cycle)


class LoggedI2cTarget(I2cTarget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stretches = []

    def _on_bus_scl_falling(self, cycle):
        super()._on_bus_scl_falling(cycle)
        self.stretches.append((cycle, cycle + self.stretch_cycles))


def observe_stretch_holds(board, scl):
    """Record external low while the chip's actual SCL pad is released.

    Observe the public board resolution inputs without changing resolution.
    Device decoders observe a resolution on the following cycle, so these
    intervals use that same cycle convention. RTL lockstep supplies RTL pads.
    """
    original = board.resolve
    holds = []

    def resolve(drives):
        nets = original(drives)
        if (drives[0].get(scl) is None and nets.get(scl) == 0 and
                any(d.get(scl) == 0 for d in drives[1:])):
            cycle = board.cycle + 1
            if holds and holds[-1][1] == cycle:
                holds[-1] = (holds[-1][0], cycle + 1)
            else:
                holds.append((cycle, cycle + 1))
        return nets

    board.resolve = resolve
    return holds


def assert_overlap(uart, coded, target, holds):
    start = next(e.cycle for e in target.events if e.kind == "start")
    stop = next(e.cycle for e in target.events if e.kind == "stop")
    # Require simultaneous Manchester payload, a UART frame and external
    # SCL low while the controller releases it, inside observed START/STOP.
    overlaps = []
    for f in coded.frames:
        for lo, hi in target.stretches:
            for u in uart.starts:
                a = max(start, f.start_cycle, lo, u)
                b = min(stop, f.start_cycle + 1600, hi, u + 500)
                if b <= a:
                    continue
                for released, resumed in holds:
                    if min(b, resumed) > max(a, released):
                        overlaps.append((max(a, released), min(b, resumed)))
    assert overlaps, "no three-protocol overlap during I2C clock stretching"
    return overlaps


def prepare(link, name):
    from tempo_host.images import IMAGES
    im = IMAGES[name]
    link.load(im)
    for _ in range(50):
        if not link.status().run & 3:
            return im
    raise AssertionError("demo initialization did not halt transmit threads")
