# SPDX-License-Identifier: Apache-2.0
"""Triggered pulse generator: a pulse on one pin at an exact delay after a
trigger seen on another (a UART byte, or the Nth edge).

Runs the "trigger" firmware image (fw/trigger.s). The pulse edges are
anchored to the trigger edge's cycle (the lane's CAP or capture timestamp),
so the delay is exact to the clock cycle; the only uncertainty is the
+-1 cycle of sampling the asynchronous trigger input (20 ns at 50 MHz).
A delay too short for the firmware to schedule (for a UART trigger, less
than the byte's own length plus about 100 cycles) raises LATE on lane 0,
which link.check() reports.
"""

from .images import IMAGES
from .link import TempoError
from .uart import bit_time

NOPIN = 15
E_D, E_DEN = 1, 16
P_I, PP_I = 2, 32
CLOCK_HZ = 50000000


class PulseTrigger:
    def __init__(self, link, out=0, trig=1, delay=5000, width=50, kind="uart", baud=115200,
                 match=0x42, mask=0xFF, count=1, edge="fall", active_high=True, filter_cycles=0):
        """out, trig: Tempo pin indices. delay, width: cycles (delay up to
        about 2^30). kind "uart": trigger on a received byte (8N1 at baud)
        equal to match under mask; kind "edge": on every count-th edge of
        the trigger pin."""
        self.link = link
        hi, lo = divmod(int(delay), 16384)
        pre, whole, frac = bit_time(baud, link.clock_hz)
        idle = E_DEN | (0 if active_high else E_D)
        link.load(IMAGES["trigger"], {
            "kind": 1 if kind == "uart" else 2,
            "outpins": NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | out,
            "idle": idle, "active": idle ^ E_D,
            "rxpins": NOPIN << 12 | NOPIN << 8 | trig << 4 | NOPIN,
            "len": whole, "frac": frac, "samp": whole // 2,
            "pat": (PP_I | P_I) << 8 | PP_I,
            "match": match, "mask": mask,
            "capmode": 1 << 7 | (filter_cycles & 7) << 9,
            "count": count, "delayhi": hi, "delaylo": lo, "width": width,
        })

    def wait(self, n=1, timeout_us=1000000):
        """Wait for n more pulses; returns the running pulse count and
        raises if a pulse was scheduled late or a trigger was dropped."""
        got = self.link.get(n, timeout_us)
        self.link.check()
        last = got[-1]
        if last >> 8:
            raise TempoError("%d trigger(s) dropped: pulses were still pending" % (last >> 8))
        return last & 0xFF
