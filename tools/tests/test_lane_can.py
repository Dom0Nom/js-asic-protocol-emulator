import pytest

from tempo.clock import CLOCK_HZ
from tempo.harness import Harness
from tempo.models.can import (CanFrame, CanReceiver, CanTransmitter, crc15, frame_bits, stuff,
                              stuff_positions)

BUS = "uio0"
ACKED, NO_ACK, LOST, ERROR = range(4)

# Stuff bits: in the identifier (0x104, 0x01F, 0x7C1), in the data (0x555,
# 0x13C), after the last CRC bit (0x114 recessive, 0x104 and 0x13C dominant).
F_DLC0_CRC_END_REC = CanFrame(0x114)
F_DLC0_CRC_END_DOM = CanFrame(0x104)
F_DLC0_ID = CanFrame(0x7C1)
F_DLC1_ID = CanFrame(0x01F, b"\x00")
F_DLC8_DATA = CanFrame(0x555, bytes([0x00, 0x00, 0xFF, 0xFF, 0xF0, 0x0F, 0x55, 0xAA]))
F_DLC8_CRC_END = CanFrame(0x13C, bytes(range(1, 9)))
FRAMES = [F_DLC0_CRC_END_REC, F_DLC0_CRC_END_DOM, F_DLC0_ID, F_DLC1_ID, F_DLC8_DATA, F_DLC8_CRC_END,
          CanFrame(0x000, bytes(8)), CanFrame(0x7FF, b"\xFF" * 8), CanFrame(0x2AA, b"\xA5\x5A\xC3")]


def harness(clock_hz=CLOCK_HZ, kbps=125):
    h = Harness(f'.equ CAN_CLK_KHZ, {int(clock_hz // 1000)}\n.equ CAN_KBPS, {kbps}\n.include "can_tx.s"\n',
                clock_hz=clock_hz, pullups={BUS})
    h.bit = clock_hz / (kbps * 1000)
    h.node = dict(bit_rate=kbps * 1000, clock_hz=clock_hz)
    h.run(200)
    return h


def submit(h, frame):
    d = list(frame.data)
    words = [frame.ident << 4 | len(d)] + [d[k] << 8 | d[k + 1] for k in range(0, len(d) - 1, 2)] + d[len(d) & ~1:]
    for w in words:
        assert h.run_until(lambda: len(h.chip.h2t) < 4, 2000)
        h.chip.h2t.append(w)


def result(h, bits=220):
    assert h.run_until(lambda: h.chip.t2h, bits * h.bit), "no status word"
    return h.chip.t2h.popleft()


def send(h, frame, bits=220):
    submit(h, frame)
    return result(h, bits)


def drive_spans(h):
    """[start, end) cycles in which the chip drove the bus, and the levels driven."""
    spans, levels, start = [], set(), None
    for cycle, val, oe in h.trace + [(h.board.cycle, 0, 0)]:
        if oe & 1 and start is None:
            start = cycle
        if oe & 1:
            levels.add(val & 1)
        if not oe & 1 and start is not None:
            spans.append((start, cycle))
            start = None
    return spans, levels


def wire_index(frame, pos):
    return [idx for _, idx in stuff(frame_bits(frame))].index(pos)


def clean(h, *nodes):
    assert not h.chip.illegal and not h.board.contention
    assert drive_spans(h)[1] <= {0}  # open drain: never drives high
    for n in nodes:
        assert not n.violations, n.violations


def test_crc15_check_value():
    bits = [(byte >> k) & 1 for byte in b"123456789" for k in range(7, -1, -1)]
    assert crc15(bits) == 0x059E  # CRC-15/CAN catalogue check value


def test_stuffing_cases_are_what_they_claim():
    for f, dom in ((F_DLC0_CRC_END_REC, 1), (F_DLC0_CRC_END_DOM, 0), (F_DLC8_CRC_END, 0)):
        bits = frame_bits(f)
        assert stuff_positions(f)[-1] == len(bits) - 1 and stuff(bits)[-1] == (dom, None)
    for f in (F_DLC0_CRC_END_DOM, F_DLC0_ID, F_DLC1_ID):
        assert any(1 <= p <= 11 for p in stuff_positions(f))
    for f in (F_DLC8_DATA, F_DLC8_CRC_END):
        assert sum(19 <= p < 19 + 64 for p in stuff_positions(f)) >= 4


def model_pair(frame, bits=None):
    h = Harness("", pullups={BUS})
    rx = h.attach(CanReceiver(BUS))
    tx = h.attach(CanTransmitter(BUS, frame, start=3000, bits=bits))
    h.run(3000 + 150 * 200)
    assert tx.result == "sent"
    return rx, tx


def test_models_against_each_other():
    for f in (F_DLC8_CRC_END, F_DLC0_CRC_END_REC, CanFrame(0x321, rtr=True, dlc=4)):
        rx, tx = model_pair(f)
        assert rx.frames == [f] and tx.acked and not rx.violations
        assert rx.frames[0].stuff_bits == len(stuff_positions(f))


def test_receiver_model_rejects_bad_frames():
    f = F_DLC8_DATA
    bad_crc = frame_bits(f)
    bad_crc[30] ^= 1
    rx, tx = model_pair(f, [b for b, _ in stuff(bad_crc)])
    assert not rx.frames and not tx.acked and [v[1] for v in rx.violations] == ["CRC error"]
    wire = stuff(frame_bits(f))
    k = [idx for _, idx in wire].index(None)
    six_equal = [b for b, _ in wire]
    six_equal[k] ^= 1
    rx, tx = model_pair(f, six_equal)
    assert not rx.frames and not tx.acked and [v[1] for v in rx.violations] == ["stuff error"]


@pytest.mark.parametrize("frame", FRAMES, ids=lambda f: f"{f.ident:03x}_dlc{f.dlc}")
def test_frame_is_received_and_acknowledged(frame):
    h = harness()
    rx = h.attach(CanReceiver(BUS, sjw=0))  # no resynchronisation: the whole frame must sit on the SOF grid
    h.run(12 * h.bit)
    assert send(h, frame) == ACKED
    assert rx.frames == [frame]
    assert rx.frames[0].stuff_bits == len(stuff_positions(frame)) and rx.frames[0].acked
    assert max(map(abs, rx.phase_errors), default=0) <= 1
    clean(h, rx)


@pytest.mark.parametrize("error,sample_point", [(-0.01, 0.75), (0.01, 0.75), (-0.01, 0.875), (0.01, 0.625)])
def test_receiver_bit_rate_error(error, sample_point):
    for frame in (F_DLC8_DATA, CanFrame(0x000, bytes(8))):
        h = harness()
        rx = h.attach(CanReceiver(BUS, bit_rate_error=error, sample_point=sample_point))
        h.run(12 * h.bit)
        assert send(h, frame) == ACKED
        assert rx.frames == [frame]
        clean(h, rx)


@pytest.mark.parametrize("clock_hz,kbps", [(30e6, 125), (25e6, 800), (30e6, 800), (25e6, 1000)])
def test_bit_time_follows_clock_and_rate(clock_hz, kbps):
    h = harness(clock_hz, kbps)
    rx = h.attach(CanReceiver(BUS, sjw=0, **h.node))
    h.run(12 * h.bit)
    assert send(h, F_DLC8_CRC_END) == ACKED
    assert rx.frames == [F_DLC8_CRC_END]
    assert max(map(abs, rx.phase_errors)) <= 2  # 31.25 and 37.5 cycles per bit use the fractional period
    clean(h, rx)


def test_no_receiver_reports_no_ack():
    h = harness()
    assert send(h, F_DLC1_ID) == NO_ACK
    clean(h)


def test_listen_only_receiver_reports_no_ack():
    h = harness()
    rx = h.attach(CanReceiver(BUS, ack=False))
    h.run(12 * h.bit)
    assert send(h, F_DLC8_CRC_END) == NO_ACK
    assert rx.frames == [F_DLC8_CRC_END] and not rx.frames[0].acked
    clean(h, rx)


def test_back_to_back_frames_keep_the_interframe_space():
    h = harness()
    rx = h.attach(CanReceiver(BUS))
    h.run(12 * h.bit)
    submit(h, F_DLC0_CRC_END_REC)
    submit(h, F_DLC1_ID)
    assert [result(h), result(h)] == [ACKED, ACKED]
    assert rx.frames == [F_DLC0_CRC_END_REC, F_DLC1_ID]
    clean(h, rx)


def test_waits_for_a_busy_bus():
    h = harness()
    rx = h.attach(CanReceiver(BUS))
    first = CanFrame(0x700, bytes(range(8)))
    other = h.attach(CanTransmitter(BUS, first, start=h.board.cycle + int(12 * h.bit)))
    h.run(40 * h.bit)  # submitted in the middle of the other node's frame
    assert send(h, F_DLC1_ID, bits=400) == ACKED
    assert other.result == "sent" and other.acked
    assert rx.frames == [first, F_DLC1_ID]
    end_of_first = other.sof_cycle + len(other.wire) * other.tbit
    assert rx.frames[1].sof_cycle - end_of_first >= 3 * h.bit
    clean(h, rx, other)


# 0x21F loses on its fifth recessive bit in a row, with a stuff bit pending in the lane.
LOSE = [(0x555, 0x155, 125), (0x123, 0x122, 125), (0x7FF, 0x7FE, 125), (0x041, 0x040, 125),
        (0x21F, 0x21E, 125), (0x123, 0x122, 1000), (0x21F, 0x21E, 1000)]


@pytest.mark.parametrize("ident,winner,kbps", LOSE, ids=[f"{a:03x}_{b:03x}_{k}k" for a, b, k in LOSE])
def test_arbitration_lost_to_a_lower_identifier(ident, winner, kbps):
    h = harness(kbps=kbps)
    rx = h.attach(CanReceiver(BUS, **h.node))
    h.run(12 * h.bit)
    mine = CanFrame(ident, b"\x12\x34\x56")
    theirs = CanFrame(winner, b"\xCA\xFE")
    other = h.attach(CanTransmitter(BUS, theirs, **h.node))
    assert send(h, mine) == LOST
    lost_pos = 11 - (ident ^ winner).bit_length() + 1
    spans, _ = drive_spans(h)
    sof = spans[0][0]
    assert spans[-1][1] <= sof + wire_index(mine, lost_pos) * h.bit + 1  # released from the lost bit on
    h.run(150 * h.bit)
    assert drive_spans(h)[0] == spans  # and nothing driven while the winner finishes
    assert other.result == "sent" and other.acked
    assert rx.frames == [theirs]
    clean(h, rx, other)
    # the stopped lane sends the next frame correctly
    assert send(h, mine) == ACKED
    assert rx.frames == [theirs, mine]
    clean(h, rx)


WIN = [(0x155, CanFrame(0x555, b"\x01"), 1), (0x122, CanFrame(0x123, b"\x01"), 11),
       (0x7FE, CanFrame(0x7FF, b"\x01"), 11), (0x040, CanFrame(0x041), 11),
       (0x123, CanFrame(0x123, rtr=True, dlc=2), 12)]


@pytest.mark.parametrize("ident,loser,lost_at", WIN, ids=lambda v: f"{v:03x}" if isinstance(v, int) else None)
def test_arbitration_won_against_a_higher_identifier(ident, loser, lost_at):
    h = harness()
    rx = h.attach(CanReceiver(BUS))
    h.run(12 * h.bit)
    mine = CanFrame(ident, b"\xDE\xAD\xBE\xEF")
    other = h.attach(CanTransmitter(BUS, loser))
    assert send(h, mine) == ACKED
    assert (other.result, other.lost_at) == ("lost", lost_at)
    assert rx.frames == [mine]
    clean(h, rx, other)


class Jam:
    """Drives the bus dominant for `length` cycles, `delay` cycles after its first falling edge."""

    def __init__(self, net, delay, length):
        self.net, self.delay, self.length = net, delay, length
        self.prev, self.edge = 1, None

    def step(self, cycle, nets):
        level = nets.get(self.net, 1)
        if self.edge is None and self.prev == 1 and level == 0:
            self.edge = cycle
        self.prev = level
        return {self.net: 0} if self.edge is not None and 0 <= cycle - self.edge - self.delay < self.length else {}


def test_dominant_bit_in_recessive_data_is_reported():
    h = harness()
    frame = CanFrame(0x123, b"\xFF\xFF")
    h.attach(Jam(BUS, int(wire_index(frame, 21) * h.bit), int(h.bit)))  # third data bit
    assert send(h, frame) == ERROR
    clean(h)
    h.run(12 * h.bit)
    rx = h.attach(CanReceiver(BUS))
    h.run(12 * h.bit)
    assert send(h, frame) == ACKED  # the error does not stick
    assert rx.frames == [frame]
