"""Classical CAN wire models: a receiving node and a transmitting node.

Written from the Bosch CAN Specification 2.0, Part A (ISO 11898-1 specifies
the same data link layer). Section numbers refer to Part A.

Bus (2, "Bus values"): the bus is a wired-AND. A node drives dominant (0)
or releases the net to the pull-up, recessive (1).

Data frame (3.1.1), in transmission order, every field MSB first:
    SOF              1 dominant bit
    identifier       11 bits
    RTR              dominant in a data frame, recessive in a remote frame
    reserved r1, r0  sent dominant; receivers accept either level
    DLC              4 bits, number of data bytes; values above 8 mean 8
    data             0-8 bytes, absent in a remote frame (3.1.2)
    CRC sequence     15 bits, generator x^15 + x^14 + x^10 + x^8 + x^7 +
                     x^4 + x^3 + 1 (0x4599), register cleared at SOF,
                     computed over the destuffed bits SOF through data
    CRC delimiter    1 recessive bit
    ACK slot         sent recessive; every receiver whose CRC check passed
                     overwrites it with a dominant bit
    ACK delimiter    1 recessive bit
    EOF              7 recessive bits
Interframe space (3.1.5): three recessive intermission bits follow EOF.
Section 9 lets a node read a dominant third intermission bit as SOF.

Coding (5): SOF through the CRC sequence is bit stuffed. After five
consecutive equal bits the transmitter inserts one complementary bit; a
stuff bit counts towards the next run, and one is inserted after the last
CRC bit when that bit ends a run of five. The CRC delimiter, the ACK field
and EOF have fixed form and are not stuffed.

Arbitration (2, "Arbitration"): transmitters that start together compare
each bit they send with the bus. A node that sends recessive and reads
dominant in the identifier or the RTR bit has lost and sends nothing more.

Error detection (6.1): a sixth equal bit in the stuffed region is a stuff
error, a dominant bit in a fixed-form recessive field is a form error (a
receiver ignores the last EOF bit, section 4), a CRC mismatch is a CRC
error, and a transmitter reading a level it did not send outside
arbitration and the ACK slot has a bit error.

Bit timing (8): a node hard-synchronises on the recessive-to-dominant edge
of SOF. On later recessive-to-dominant edges it measures the phase error
against its own bit boundary and corrects by at most the resynchronisation
jump width: an edge after the boundary delays the sample point, an edge
before the boundary shortens the current bit. At most one correction is
made between two sample points, and only when the previous sample was
recessive.

Limits of these models: base frame format only, no error or overload
frames (a node that detects an error records it, goes quiet and waits for
11 recessive bit times), no fault confinement, no retransmission.
"""

from dataclasses import dataclass, field

from ..clock import CLOCK_HZ

CRC15_POLY = 0x4599
ARBITRATION_BITS = 13  # SOF, identifier, RTR
HEADER_BITS = 19  # through DLC


@dataclass
class CanFrame:
    ident: int
    data: bytes = b""
    rtr: bool = False
    dlc: int = None
    stuff_bits: int = field(default=0, compare=False)
    acked: bool = field(default=False, compare=False)
    sof_cycle: int = field(default=None, compare=False)

    def __post_init__(self):
        self.data = bytes(self.data)
        if self.dlc is None:
            self.dlc = len(self.data)
        if not 0 <= self.ident < 1 << 11:
            raise ValueError("identifier is 11 bits")
        if self.rtr and self.data:
            raise ValueError("a remote frame has no data field")
        if not self.rtr and len(self.data) != min(self.dlc, 8):
            raise ValueError("DLC does not match the data length")


def crc15(bits):
    reg = 0
    for b in bits:
        feedback = b ^ (reg >> 14)
        reg = (reg << 1) & 0x7FFF
        if feedback:
            reg ^= CRC15_POLY
    return reg


def _msb_first(value, width):
    return [(value >> k) & 1 for k in range(width - 1, -1, -1)]


def frame_bits(frame):
    """Destuffed bits of a frame, SOF through the CRC sequence."""
    bits = [0] + _msb_first(frame.ident, 11) + [int(frame.rtr), 0, 0] + _msb_first(frame.dlc, 4)
    for byte in frame.data:
        bits += _msb_first(byte, 8)
    return bits + _msb_first(crc15(bits), 15)


def stuff(bits):
    """[(level, index)]: the wire bits with stuff bits inserted. index is the
    position in `bits`, or None for a stuff bit."""
    out = []
    last, run = None, 0
    for k, b in enumerate(bits):
        run = run + 1 if b == last else 1
        last = b
        out.append((b, k))
        if run == 5:
            last, run = 1 - b, 1
            out.append((last, None))
    return out


def stuff_positions(frame):
    """Indices of the destuffed bits that are followed by a stuff bit."""
    wire = stuff(frame_bits(frame))
    return [wire[k - 1][1] for k, (_, idx) in enumerate(wire) if idx is None]


class _Node:
    def __init__(self, net, bit_rate, clock_hz, bit_rate_error, sample_point):
        self.net = net
        self.tbit = clock_hz / (bit_rate * (1.0 + bit_rate_error))
        self.sample_point = sample_point
        self.violations = []  # (cycle, description)
        self.prev = 1
        self.bit_start = 0.0
        self.bit_end = 0.0
        self.sampled = False

    def _begin_bit(self, start):
        self.bit_start = start
        self.bit_end = start + self.tbit
        self.sampled = False

    def _sample_due(self, cycle):
        return not self.sampled and cycle >= self.bit_start + self.sample_point * self.tbit


class CanReceiver(_Node):
    """A receiving node on `net`.

    frames: CanFrame for every frame received without error, appended at
        the last but one EOF bit. stuff_bits counts the stuff bits removed,
        acked is the ACK slot level seen, sof_cycle the SOF edge.
    violations: (cycle, text) for stuff, form and CRC errors, a dominant
        SOF that does not last to the sample point, and a frame started
        inside the first two intermission bits.
    phase_errors: signed distance in cycles of every recessive-to-dominant
        edge after SOF from the node's bit boundary (positive: edge late).

    ack=False listens without driving the ACK slot. sjw is the
    resynchronisation jump width as a fraction of the bit time; 0 leaves
    the node on the grid set by its hard synchronisation.
    """

    def __init__(self, net, bit_rate=125_000, clock_hz=CLOCK_HZ, bit_rate_error=0.0,
                 sample_point=0.75, sjw=0.125, ack=True):
        super().__init__(net, bit_rate, clock_hz, bit_rate_error, sample_point)
        self.sjw = sjw * self.tbit
        self.ack = ack
        self.frames = []
        self.phase_errors = []
        self.state = "integrate"
        self.recessive_since = None
        self.earliest_sof = 0.0
        self.driving = False
        self.drive_next = False

    def step(self, cycle, nets):
        level = nets.get(self.net, 1)
        edge = self.prev == 1 and level == 0
        self.prev = level
        if self.state == "integrate":
            if level == 0:
                self.recessive_since = None
            elif self.recessive_since is None:
                self.recessive_since = cycle
            elif cycle - self.recessive_since >= 11 * self.tbit:
                self.state = "idle"
        elif self.state == "idle":
            if edge:
                if cycle < self.earliest_sof:
                    self._fail(cycle, "frame started during intermission")
                else:
                    self._start_frame(cycle)
        else:
            if cycle >= self.bit_end:
                self._begin_bit(self.bit_end)
                self.driving, self.drive_next = self.drive_next, False
            if edge and self.last_sample == 1 and not self.synced and not self.driving:
                self._resync(cycle)
            if self._sample_due(cycle):
                self.sampled = True
                self.synced = False
                self.last_sample = level
                self._on_bit(level, cycle)
        return {self.net: 0} if self.driving and self.state == "frame" else {}

    def _resync(self, cycle):
        self.synced = True
        if not self.sampled:
            err = cycle - self.bit_start
            shift = min(err, self.sjw)
            self.bit_start += shift
            self.bit_end += shift
        else:
            err = cycle - self.bit_end
            self.bit_end -= min(-err, self.sjw)
        self.phase_errors.append(err)

    def _fail(self, cycle, text):
        self.violations.append((cycle, text))
        self.state = "integrate"
        self.recessive_since = None
        self.driving = self.drive_next = False

    def _start_frame(self, cycle):
        self.state = "frame"
        self.field = "stuffed"
        self.sof_cycle = cycle
        self._begin_bit(cycle)
        self.synced = True
        self.last_sample = 1
        self.bits = []
        self.total = None
        self.run_level, self.run = None, 0
        self.stuff_bits = 0
        self.count = 0

    def _on_bit(self, level, cycle):
        f = self.field
        if f == "stuffed":
            if self.run == 5:
                if level == self.run_level:
                    return self._fail(cycle, "stuff error")
                self.run_level, self.run = level, 1
                self.stuff_bits += 1
            else:
                if not self.bits and level:
                    return self._fail(cycle, "SOF shorter than the sample point")
                self.run = self.run + 1 if level == self.run_level else 1
                self.run_level = level
                self.bits.append(level)
                if len(self.bits) == HEADER_BITS:
                    dlc = int("".join(map(str, self.bits[15:19])), 2)
                    self.total = HEADER_BITS + (0 if self.bits[12] else 8 * min(dlc, 8)) + 15
            if len(self.bits) == self.total and self.run < 5:
                self.field = "crc_delimiter"
        elif f == "crc_delimiter":
            if level == 0:
                return self._fail(cycle, "form error: CRC delimiter")
            body, seq = self.bits[:-15], self.bits[-15:]
            self.crc_ok = crc15(body) == int("".join(map(str, seq)), 2)
            self.drive_next = self.crc_ok and self.ack
            self.field = "ack_slot"
        elif f == "ack_slot":
            self.acked = level == 0
            self.field = "ack_delimiter"
        elif f == "ack_delimiter":
            if level == 0:
                return self._fail(cycle, "form error: ACK delimiter")
            if not self.crc_ok:
                return self._fail(cycle, "CRC error")
            self.field = "eof"
        else:
            self.count += 1
            if self.count <= 6 and level == 0:
                return self._fail(cycle, f"form error: EOF bit {self.count}")
            if self.count == 6:
                self.frames.append(self._decoded())
            if self.count == 7:
                self.state = "idle"
                self.earliest_sof = self.bit_end + 2 * self.tbit - self.sjw

    def _decoded(self):
        b = self.bits
        num = lambda part: int("".join(map(str, part)), 2)  # noqa: E731
        rtr = bool(b[12])
        dlc = num(b[15:19])
        n = 0 if rtr else min(dlc, 8)
        data = bytes(num(b[19 + 8 * k:27 + 8 * k]) for k in range(n))
        return CanFrame(num(b[1:12]), data, rtr, dlc, stuff_bits=self.stuff_bits,
                        acked=self.acked, sof_cycle=self.sof_cycle)


class CanTransmitter(_Node):
    """A transmitting node that sends one frame.

    start=None: the node has the frame pending on an idle bus. When another
    node's SOF edge appears it hard-synchronises on that edge, drives
    dominant for the rest of that bit and sends its identifier from the
    next bit, so both frames arbitrate. start=<cycle>: it starts its own
    SOF in that cycle.

    bits overrides the stuffed wire bits SOF through the CRC sequence (for
    sending malformed frames).

    result: None while pending or sending, then "sent", "lost" (lost_at is
        the destuffed bit index, 1 for the first identifier bit, 12 for
        RTR) or "error" (a bit error, recorded in violations).
    acked: the ACK slot was dominant.
    """

    def __init__(self, net, frame, bit_rate=125_000, clock_hz=CLOCK_HZ, bit_rate_error=0.0,
                 sample_point=0.75, start=None, bits=None):
        super().__init__(net, bit_rate, clock_hz, bit_rate_error, sample_point)
        self.frame = frame
        self.start = start
        wire = stuff(frame_bits(frame)) if bits is None else [(b, None) for b in bits]
        self.ack_index = len(wire) + 1
        self.wire = wire + [(1, None)] * 10  # CRC delimiter, ACK slot, ACK delimiter, EOF
        self.result = None
        self.lost_at = None
        self.acked = False
        self.sof_cycle = None
        self.index = None

    def step(self, cycle, nets):
        level = nets.get(self.net, 1)
        edge = self.prev == 1 and level == 0
        self.prev = level
        if self.result is not None:
            return {}
        if self.index is None:
            if (self.start is None and edge) or (self.start is not None and cycle >= self.start):
                self.index = 0
                self.sof_cycle = cycle
                self._begin_bit(cycle)
            else:
                return {}
        if cycle >= self.bit_end:
            self._begin_bit(self.bit_end)
            self.index += 1
            if self.index == len(self.wire):
                self.result = "sent"
                return {}
        sent, pos = self.wire[self.index]
        if self._sample_due(cycle):
            self.sampled = True
            if self.index == self.ack_index:
                self.acked = level == 0
            elif sent == 1 and level == 0 and pos is not None and pos < ARBITRATION_BITS:
                self.result, self.lost_at = "lost", pos
                return {}
            elif sent != level:
                self.result = "error"
                self.violations.append((cycle, f"bit error at wire bit {self.index}"))
                return {}
        return {self.net: 0} if sent == 0 else {}
