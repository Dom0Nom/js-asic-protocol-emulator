"""PS/2 keyboard/mouse wire models, device-to-host direction.

Reference: Adam Chapweske, "The PS/2 Mouse/Keyboard Protocol" (1999),
https://www.burtonsys.com/ps2_chapweske.htm, sections "Communication:
General Description" and "Communication: Device-to-Host":

  * CLOCK and DATA are open collector with pull-ups; the bus idles with
    both high. The device always generates the clock.
  * Frame: start bit 0, 8 data bits LSB first, odd parity bit (data plus
    parity hold an odd number of ones), stop bit 1.
  * The host reads DATA on the falling edge of CLOCK; the device changes
    DATA while CLOCK is high.
  * Clock frequency 10 - 16.7 kHz: period 60 - 100 us, high for 30 - 50 us
    and low for 30 - 50 us.
  * From the rising clock edge to a DATA transition: at least 5 us.
  * From a DATA transition to the falling clock edge: at least 5 us and at
    most 25 us.

Times are measured in board cycles. An edge is located to one cycle, so
every limit is applied with one cycle of tolerance.
"""

from collections import deque
from ..clock import CLOCK_HZ

PERIOD_US = (60.0, 100.0)
PHASE_US = (30.0, 50.0)
SETUP_US = (5.0, 25.0)
HOLD_US = 5.0
FRAME_BITS = 11
_NEVER = -(1 << 60)


def odd_parity(byte):
    """Parity bit that makes the nine bits hold an odd number of ones."""
    return 1 ^ (bin(byte & 0xFF).count("1") & 1)


class Ps2Violation:
    __slots__ = ("cycle", "kind", "detail")

    def __init__(self, cycle, kind, detail=""):
        self.cycle = cycle
        self.kind = kind
        self.detail = detail

    def __repr__(self):
        return f"Ps2Violation(cycle={self.cycle}, kind={self.kind!r}, detail={self.detail!r})"


class Ps2Host:
    """Host-side checker for frames a device sends.

    bytes: data byte of every completed frame, including frames with
        errors (the violation records what was wrong).
    frames: (cycle of the stop-bit clock edge, raw 11-bit frame, bit 0 the
        start bit).
    violations: list of Ps2Violation. Kinds: 'period', 'clock_low',
        'clock_high' (clock outside 10 - 16.7 kHz or a phase outside
        30 - 50 us), 'setup' (DATA transition less than 5 us or more than
        25 us before the falling edge), 'hold' (DATA transition while the
        clock is low inside a frame, or less than 5 us after the rising
        edge), 'start', 'parity', 'stop', 'incomplete' (the clock stopped
        inside a frame).
    """

    def __init__(self, clock, data, clock_hz=CLOCK_HZ):
        self.clock = clock
        self.data = data
        self.cycles_per_us = clock_hz / 1e6
        self.bytes = []
        self.frames = []
        self.violations = []
        self._prev_c = 1
        self._prev_d = 1
        self._bits = []
        self._active = False  # from the start-bit clock edge to the rise after the stop bit
        self._last_fall = _NEVER
        self._last_rise = _NEVER
        self._last_change = _NEVER

    def _us(self, cycles):
        return cycles / self.cycles_per_us

    def _flag(self, cycle, kind, detail=""):
        self.violations.append(Ps2Violation(cycle, kind, detail))

    def _check(self, cycle, kind, cycles, lo_us, hi_us):
        lo = lo_us * self.cycles_per_us - 1
        hi = hi_us * self.cycles_per_us + 1
        if not lo <= cycles <= hi:
            self._flag(cycle, kind, f"{self._us(cycles):.2f} us, allowed {lo_us:g} - {hi_us:g}")

    def step(self, cycle, nets):
        c = nets.get(self.clock, 1)
        d = nets.get(self.data, 1)

        if self._bits and cycle - self._last_fall > 2 * PERIOD_US[1] * self.cycles_per_us:
            self._flag(cycle, "incomplete", f"{len(self._bits)} bits")
            self._bits = []
            self._active = False

        if d != self._prev_d:
            if c == 0 or self._prev_c == 0:
                if self._active:
                    self._flag(cycle, "hold", "DATA changed while CLOCK low")
            elif cycle - self._last_rise < HOLD_US * self.cycles_per_us - 1:
                self._flag(cycle, "hold", f"{self._us(cycle - self._last_rise):.2f} us after the rising edge")
            self._last_change = cycle

        if self._prev_c == 1 and c == 0:
            self._on_fall(cycle, d)
        elif self._prev_c == 0 and c == 1:
            if self._active:
                self._check(cycle, "clock_low", cycle - self._last_fall, *PHASE_US)
                if not self._bits:
                    self._active = False
            self._last_rise = cycle

        self._prev_c, self._prev_d = c, d
        return {}

    def _on_fall(self, cycle, d):
        if self._bits:
            self._check(cycle, "period", cycle - self._last_fall, *PERIOD_US)
            self._check(cycle, "clock_high", cycle - self._last_rise, *PHASE_US)
        setup = cycle - self._last_change
        if setup < SETUP_US[0] * self.cycles_per_us - 1:
            self._flag(cycle, "setup", f"{self._us(setup):.2f} us before the falling edge")
        elif self._last_change > self._last_rise and setup > SETUP_US[1] * self.cycles_per_us + 1:
            self._flag(cycle, "setup", f"{self._us(setup):.2f} us before the falling edge, at most {SETUP_US[1]:g}")
        self._last_fall = cycle
        self._active = True
        self._bits.append(d)
        if len(self._bits) == FRAME_BITS:
            self._finish(cycle)

    def _finish(self, cycle):
        bits, self._bits = self._bits, []
        byte = sum(b << k for k, b in enumerate(bits[1:9]))
        if bits[0] != 0:
            self._flag(cycle, "start", "start bit high")
        if bits[9] != odd_parity(byte):
            self._flag(cycle, "parity", f"byte {byte:#04x} with parity bit {bits[9]}")
        if bits[10] != 1:
            self._flag(cycle, "stop", "stop bit low")
        self.frames.append((cycle, sum(b << k for k, b in enumerate(bits))))
        self.bytes.append(byte)


class Ps2Device:
    """Device-side transmitter: drives open-drain CLOCK and DATA.

    period_us: clock period, 60 (16.7 kHz) to 100 (10 kHz); the clock is
        low for half of it.
    setup_us: DATA transition to falling clock edge, 5 to 25 and at least
        5 us after the preceding rising edge.
    gap_us: idle time between frames (default: one clock period).
    """

    def __init__(self, clock, data, period_us=80.0, setup_us=15.0, gap_us=None, clock_hz=CLOCK_HZ):
        if not PERIOD_US[0] <= period_us <= PERIOD_US[1]:
            raise ValueError(f"clock period {period_us} us outside {PERIOD_US}")
        if not SETUP_US[0] <= setup_us <= min(SETUP_US[1], period_us / 2 - HOLD_US):
            raise ValueError(f"setup {setup_us} us not legal for a {period_us} us clock")
        cyc = clock_hz / 1e6
        self.clock = clock
        self.data = data
        self.period = round(period_us * cyc)
        self.low = round(period_us * cyc / 2)
        self.setup = round(setup_us * cyc)
        self.gap = round((period_us if gap_us is None else gap_us) * cyc)
        self.sent = 0
        self._queue = deque()
        self._events = deque()  # (cycle, clock level, data level)
        self._c = 1
        self._d = 1
        self._free_at = 0

    @property
    def busy(self):
        return bool(self._queue or self._events)

    def send(self, data, bad_parity=False, bad_stop=False):
        """Queue bytes; bad_parity inverts the parity bit of each of them."""
        for byte in bytes(data):
            p = odd_parity(byte) ^ int(bad_parity)
            self._queue.append([0] + [(byte >> k) & 1 for k in range(8)] + [p, 0 if bad_stop else 1])

    def step(self, cycle, nets):
        if not self._events and self._queue and cycle >= self._free_at:
            t = cycle
            for b in self._queue.popleft():
                self._events.append((t, 1, b))
                self._events.append((t + self.setup, 0, b))
                self._events.append((t + self.setup + self.low, 1, b))
                t += self.period
            self._events.append((t, 1, 1))
            self._free_at = t + self.gap
            self.sent += 1
        while self._events and self._events[0][0] <= cycle:
            _, self._c, self._d = self._events.popleft()
        return {self.clock: None if self._c else 0, self.data: None if self._d else 0}
