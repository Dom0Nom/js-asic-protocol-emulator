"""UART transmit/receive models.

UartRx runs entirely on its own baud clock: it detects the start edge and
then samples at fixed multiples of its own bit period. It never adapts to
the transmitter, so a DUT with the wrong baud rate shows up as bad samples,
framing errors, or a measured bit period that does not match the nominal
one.
"""

from collections import deque


def _parity_bit(ones, parity):
    """Value of the parity bit that gives the requested total parity."""
    if parity == 'even':
        return ones % 2
    if parity == 'odd':
        return (ones + 1) % 2
    raise ValueError(f"bad parity kind {parity!r}")


class UartError:
    __slots__ = ("cycle", "kind", "detail")

    def __init__(self, cycle, kind, detail=""):
        self.cycle = cycle
        self.kind = kind
        self.detail = detail

    def __repr__(self):
        return f"UartError(cycle={self.cycle}, kind={self.kind!r}, detail={self.detail!r})"

    def __eq__(self, other):
        if not isinstance(other, UartError):
            return NotImplemented
        return (self.cycle, self.kind, self.detail) == (other.cycle, other.kind, other.detail)


class UartRx:
    """UART line decoder, idle high, start bit low.

    bytes: list of decoded values, appended when a frame completes (even if
        it has an error, matching how real UART hardware still delivers
        the byte and flags the error separately).
    errors: list of UartError, kinds are 'glitch' (start bit did not hold
        low to mid-bit), 'framing' (stop bit sampled low), 'parity'.
    bit_periods: one entry per completed frame, the measured average bit
        period in cycles (from real edges on the wire), or None if the
        frame had no interior transitions to measure from.
    """

    def __init__(self, net, baud, bits=8, parity=None, stop=1, clock_hz=50e6):
        self.net = net
        self.baud = baud
        self.bits = bits
        self.parity = parity
        self.stop = stop
        self.cycles_per_bit = clock_hz / baud

        self.bytes = []
        self.errors = []
        self.bit_periods = []

        self._prev_level = 1
        self._in_frame = False
        self._edges = []
        self._next_sample = 0.0
        self._slot = 0
        self._data_bits = []
        self._parity_sample = None
        self._total_slots = 1 + bits + (1 if parity else 0) + stop
        self._parity_slot = bits + 1 if parity else None

    def step(self, cycle, nets):
        level = nets.get(self.net, 1)

        if level != self._prev_level:
            if self._in_frame:
                self._edges.append((cycle, level))
            elif self._prev_level == 1 and level == 0:
                self._start_frame(cycle)

        if self._in_frame and cycle >= self._next_sample:
            self._sample(cycle, level)

        self._prev_level = level
        return {}

    def _start_frame(self, cycle):
        self._in_frame = True
        self._edges = [(cycle, 0)]
        self._slot = 0
        self._data_bits = []
        self._parity_sample = None
        self._next_sample = cycle + 0.5 * self.cycles_per_bit

    def _sample(self, cycle, level):
        slot = self._slot

        if slot == 0:
            if level != 0:
                self.errors.append(UartError(cycle, 'glitch', 'start bit not low at mid-bit'))
                self._in_frame = False
                return
        elif 1 <= slot <= self.bits:
            self._data_bits.append(level)
        elif self._parity_slot is not None and slot == self._parity_slot:
            self._parity_sample = level
        else:
            if level != 1:
                self.errors.append(UartError(cycle, 'framing', 'stop bit sampled low'))

        self._slot += 1
        self._next_sample += self.cycles_per_bit

        if self._slot >= self._total_slots:
            self._finish_frame(cycle)

    def _finish_frame(self, cycle):
        value = 0
        for i, b in enumerate(self._data_bits):
            value |= (b << i)
        if self.parity:
            expected = _parity_bit(sum(self._data_bits), self.parity)
            if self._parity_sample != expected:
                self.errors.append(
                    UartError(cycle, 'parity', f'expected {expected} got {self._parity_sample}'))
        self.bytes.append(value)
        self.bit_periods.append(self._measure_period())
        self._in_frame = False

    def _measure_period(self):
        edges = self._edges
        if len(edges) < 2:
            return None
        span = edges[-1][0] - edges[0][0]
        slots = round(span / self.cycles_per_bit)
        if slots <= 0:
            return None
        return span / slots


class UartTx(object):
    """Sends a queue of bytes, idle high.

    baud_error: fractional deliberate baud mismatch, e.g. 0.02 for +2%.
    Useful for feeding a UartRx a signal at the wrong rate.
    """

    def __init__(self, net, baud, bits=8, parity=None, stop=1, clock_hz=50e6, baud_error=0.0):
        self.net = net
        self.bits = bits
        self.parity = parity
        self.stop = stop
        self.cycles_per_bit = clock_hz / (baud * (1 + baud_error))

        self._queue = deque()
        self._level = 1
        self._phase_start = 0.0
        self._active = False

    def send(self, data, gap_bits=0):
        for byte in data:
            frame = [0]
            ones = 0
            for i in range(self.bits):
                b = (byte >> i) & 1
                frame.append(b)
                ones += b
            if self.parity:
                frame.append(_parity_bit(ones, self.parity))
            frame.extend([1] * self.stop)
            frame.extend([1] * gap_bits)
            self._queue.extend(frame)

    def step(self, cycle, nets):
        if not self._active:
            if not self._queue:
                self._level = 1
                return {self.net: 1}
            self._level = self._queue.popleft()
            self._phase_start = cycle
            self._active = True
            return {self.net: self._level}

        if cycle >= self._phase_start + self.cycles_per_bit:
            self._phase_start += self.cycles_per_bit
            if self._queue:
                self._level = self._queue.popleft()
            else:
                self._active = False
                self._level = 1

        return {self.net: self._level}
