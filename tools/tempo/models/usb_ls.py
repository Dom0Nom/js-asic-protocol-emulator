"""USB low-speed (1.5 Mbit/s) receiver/transmitter models.

Low-speed line coding (USB 2.0 spec chapter 7): idle is J. For low speed,
J is D+ low / D- high (the opposite polarity from full speed, because the
pull-up sits on D- instead of D+). K is D+ high / D- low. SE0 is both low.

NRZI: a data bit of 0 causes a transition (J<->K) on the wire; a data bit
of 1 causes no transition. SOP is the idle-J to K transition that starts
the SYNC field. SYNC is KJKJKJKK, i.e. NRZI-decoded bits 00000001.

Bit stuffing: after six consecutive 1 bits (post-SYNC, pre-NRZI value) a 0
is stuffed in and must be removed by the receiver; this guarantees a wire
transition at least every 7 bit times, which is what lets a receiver's
DPLL resync often enough to tolerate the spec's clock tolerance.

Clock tolerance for low speed is +/-1.5% (USB 2.0 spec section 7.1.11,
"Low-/Full-Speed Source Jitter" / device clock tolerance for low-speed
functions). This model's receiver does not need to know the tolerance
number itself: its DPLL just resyncs on every wire transition, and
bit-stuffing bounds how long it ever free-runs, so it tolerates whatever
drift the transmitter is configured with as long as stuffing holds.
"""

from collections import deque

# USB PIDs we support (low speed doesn't need high-speed-only ones).
# PID<3:0> values are standard (see USB 2.0 spec Table 8-1).
_PID_TABLE = {
    0b0001: ('OUT', 'token'),
    0b1001: ('IN', 'token'),
    0b0101: ('SOF', 'token'),
    0b1101: ('SETUP', 'token'),
    0b0011: ('DATA0', 'data'),
    0b1011: ('DATA1', 'data'),
    0b0010: ('ACK', 'handshake'),
    0b1010: ('NAK', 'handshake'),
    0b1110: ('STALL', 'handshake'),
}
_PID_VALUES = {name: val for val, (name, _kind) in _PID_TABLE.items()}

_SYMBOL_PINS = {'J': (0, 1), 'K': (1, 0), 'SE0': (0, 0), 'SE1': (1, 1)}
_SYNC_BITS = [0, 0, 0, 0, 0, 0, 0, 1]


def _line_state(dp, dm):
    if dp == 0 and dm == 1:
        return 'J'
    if dp == 1 and dm == 0:
        return 'K'
    if dp == 0 and dm == 0:
        return 'SE0'
    return 'SE1'


# ---------------------------------------------------------------------
# CRC5 / CRC16, each computed two independent ways and cross-checked.
# Both are "CRC-5/USB" and "CRC-16/USB" in the usual reflected-CRC sense:
# poly given in its normal (non-reflected) form, refin=refout=true.
# ---------------------------------------------------------------------

def _crc_bitserial(bits, poly_reflected, init, xorout, width):
    """Reflected bit-serial LFSR: standard technique for a refin/refout
    CRC, e.g. the inner loop of the classic reflected CRC-32 routine
    generalized to arbitrary width and applied one bit at a time."""
    reg = init
    mask = (1 << width) - 1
    for bit in bits:
        lsb = (reg & 1) ^ (bit & 1)
        reg >>= 1
        if lsb:
            reg ^= poly_reflected
    return (reg ^ xorout) & mask


def _crc5_listreg(bits):
    """Same CRC-5/USB algorithm, deliberately written as an explicit
    5-flip-flop shift register with named feedback taps instead of
    integer bit tricks, so a transcription bug in one form is unlikely
    to reappear identically in the other."""
    reg = [1, 1, 1, 1, 1]  # init 0x1F, reg[0] = LSB
    taps = (2, 4)  # poly 0x05 (x^5+x^2+1) reflected -> taps at bits 2 and 4
    for bit in bits:
        fb = reg[0] ^ (bit & 1)
        reg = reg[1:] + [0]
        if fb:
            for t in taps:
                reg[t] ^= 1
    val = sum(b << i for i, b in enumerate(reg))
    return val ^ 0x1F  # xorout


def crc5(bits):
    """CRC5/USB over an 11-bit token field, bits given LSB-first in
    transmission order. Returns the 5-bit residual, bit0 = first
    transmitted CRC bit."""
    bits = list(bits)
    a = _crc_bitserial(bits, poly_reflected=0x14, init=0x1F, xorout=0x1F, width=5)
    b = _crc5_listreg(bits)
    assert a == b, f"crc5 cross-check mismatch: {a:#04x} vs {b:#04x}"
    return a


def _crc16_table():
    table = []
    for byte in range(256):
        reg = byte
        for _ in range(8):
            reg = (reg >> 1) ^ 0xA001 if reg & 1 else reg >> 1
        table.append(reg)
    return table


_CRC16_TABLE = _crc16_table()


def _crc16_table_driven(data_bytes, init=0xFFFF, xorout=0xFFFF):
    """Sarwate table-driven byte-at-a-time CRC: a structurally different
    implementation technique from the bit-serial LFSR, used only to
    cross-check it."""
    reg = init
    for byte in data_bytes:
        reg = _CRC16_TABLE[(reg ^ byte) & 0xFF] ^ (reg >> 8)
    return reg ^ xorout


def crc16(data_bytes):
    """CRC16/USB over a byte string. Returns the 16-bit residual, bit0 =
    first transmitted CRC bit (low byte sent first, each byte LSB first,
    same convention as USB data bytes)."""
    data_bytes = bytes(data_bytes)
    bits = []
    for byte in data_bytes:
        bits.extend((byte >> i) & 1 for i in range(8))
    a = _crc_bitserial(bits, poly_reflected=0xA001, init=0xFFFF, xorout=0xFFFF, width=16)
    b = _crc16_table_driven(data_bytes)
    assert a == b, f"crc16 cross-check mismatch: {a:#06x} vs {b:#06x}"
    return a


class UsbError:
    __slots__ = ("cycle", "kind", "detail")

    def __init__(self, cycle, kind, detail=""):
        self.cycle = cycle
        self.kind = kind
        self.detail = detail

    def __repr__(self):
        return f"UsbError(cycle={self.cycle}, kind={self.kind!r}, detail={self.detail!r})"


class UsbPacket:
    def __init__(self, cycle, pid, crc_ok=True, addr=None, endp=None, frame_num=None, data=None):
        self.cycle = cycle
        self.pid = pid
        self.crc_ok = crc_ok
        self.addr = addr
        self.endp = endp
        self.frame_num = frame_num
        self.data = data

    def __repr__(self):
        return (f"UsbPacket(pid={self.pid!r}, addr={self.addr}, endp={self.endp}, "
                f"frame_num={self.frame_num}, data={self.data!r}, crc_ok={self.crc_ok})")


class UsbLsReceiver:
    """Passive low-speed receiver: SOP/NRZI/destuff/EOP, SYNC and PID
    checks, CRC5/CRC16 checks. Bit sampling uses a simple DPLL: every wire
    transition resyncs the sample phase to the transition's cycle plus
    half a nominal bit period; between transitions it free-runs on its
    own clock_hz/1.5e6 nominal bit period.
    """

    def __init__(self, dp, dm, clock_hz=50e6):
        self.dp = dp
        self.dm = dm
        self.cycles_per_bit = clock_hz / 1.5e6

        self.packets = []
        self.errors = []

        self._prev_state = 'J'
        self._frame = 'idle'  # idle | sync | pid | payload | wait_eop
        self._next_sample = 0.0
        self._last_sample_state = 'J'
        self._sync_bits = []
        self._ones_run = 0
        self._field_bits = []
        self._pid_name = None
        self._kind = None
        self._pending = {}
        self._se0_start = None
        self._start_cycle = None

    def step(self, cycle, nets):
        dp = nets.get(self.dp, 0)
        dm = nets.get(self.dm, 1)
        state = _line_state(dp, dm)

        if self._frame == 'idle':
            if self._prev_state == 'J' and state == 'K':
                self._start_packet(cycle)
        else:
            if state == 'SE0':
                if self._se0_start is None:
                    self._se0_start = cycle
            elif self._se0_start is not None:
                duration = cycle - self._se0_start
                self._se0_start = None
                if state == 'J' and duration >= 0.5 * self.cycles_per_bit:
                    self._finish_packet(cycle)
                else:
                    self.errors.append(UsbError(cycle, 'eop_error',
                                                 f'SE0 {duration} cycles then {state}'))
                    self._reset_idle()
            else:
                if state != self._prev_state:
                    self._next_sample = cycle + self.cycles_per_bit / 2
                if cycle >= self._next_sample:
                    bit = 0 if state != self._last_sample_state else 1
                    self._last_sample_state = state
                    self._next_sample += self.cycles_per_bit
                    self._consume_bit(bit, cycle)

        self._prev_state = state
        return {}

    def _start_packet(self, cycle):
        self._frame = 'sync'
        self._next_sample = cycle + self.cycles_per_bit / 2
        self._last_sample_state = 'J'
        self._sync_bits = []
        self._ones_run = 0
        self._field_bits = []
        self._pid_name = None
        self._kind = None
        self._pending = {}
        self._se0_start = None
        self._start_cycle = cycle

    def _reset_idle(self):
        self._frame = 'idle'
        self._se0_start = None

    def _consume_bit(self, bit, cycle):
        if self._frame == 'sync':
            self._sync_bits.append(bit)
            if len(self._sync_bits) == 8:
                if self._sync_bits != _SYNC_BITS:
                    self.errors.append(UsbError(cycle, 'sync_error', f'{self._sync_bits}'))
                    self._reset_idle()
                    return
                self._frame = 'pid'
                self._ones_run = 0
                self._field_bits = []
            return

        # destuffing applies to everything after SYNC
        if self._ones_run == 6:
            if bit != 0:
                self.errors.append(UsbError(cycle, 'stuff_error', 'no stuffed 0 after six ones'))
                self._reset_idle()
                return
            self._ones_run = 0
            return
        self._ones_run = self._ones_run + 1 if bit == 1 else 0

        self._field_bits.append(bit)

        if self._frame == 'pid':
            if len(self._field_bits) == 8:
                self._on_pid_byte(cycle)
        elif self._frame == 'payload' and self._kind == 'token':
            if len(self._field_bits) == 16:
                self._finalize_token(cycle)

    def _on_pid_byte(self, cycle):
        bits = self._field_bits
        pid_nibble = sum(b << i for i, b in enumerate(bits[:4]))
        check_nibble = sum(b << i for i, b in enumerate(bits[4:8]))
        if check_nibble != (~pid_nibble & 0xF):
            self.errors.append(UsbError(cycle, 'pid_error',
                                         f'pid={pid_nibble:#x} check={check_nibble:#x}'))
            self._reset_idle()
            return
        info = _PID_TABLE.get(pid_nibble)
        if info is None:
            self.errors.append(UsbError(cycle, 'pid_error', f'unknown pid {pid_nibble:#x}'))
            self._reset_idle()
            return
        self._pid_name, self._kind = info
        self._field_bits = []
        self._frame = 'wait_eop' if self._kind == 'handshake' else 'payload'

    def _finalize_token(self, cycle):
        payload_bits = self._field_bits[:11]
        crc_bits = self._field_bits[11:16]
        received = sum(b << i for i, b in enumerate(crc_bits))
        computed = crc5(payload_bits)
        self._pending['crc_ok'] = computed == received
        self._pending['payload_bits'] = payload_bits
        self._frame = 'wait_eop'

    def _finish_packet(self, cycle):
        pid = self._pid_name
        kind = self._kind
        kw = {'cycle': self._start_cycle, 'pid': pid, 'crc_ok': True}

        if kind == 'token':
            payload_bits = self._pending.get('payload_bits')
            kw['crc_ok'] = self._pending.get('crc_ok', False)
            if payload_bits is None:
                kw['crc_ok'] = False
            elif pid == 'SOF':
                kw['frame_num'] = sum(b << i for i, b in enumerate(payload_bits))
            else:
                kw['addr'] = sum(b << i for i, b in enumerate(payload_bits[:7]))
                kw['endp'] = sum(b << i for i, b in enumerate(payload_bits[7:11]))
        elif kind == 'data':
            nbits = len(self._field_bits)
            if nbits % 8 != 0 or nbits < 16:
                self.errors.append(UsbError(cycle, 'data_length_error', f'{nbits} bits'))
                kw['crc_ok'] = False
                kw['data'] = b''
            else:
                byte_vals = []
                for i in range(nbits // 8):
                    chunk = self._field_bits[i * 8:(i + 1) * 8]
                    byte_vals.append(sum(b << j for j, b in enumerate(chunk)))
                data_bytes = bytes(byte_vals[:-2])
                crc_received = byte_vals[-2] | (byte_vals[-1] << 8)
                kw['data'] = data_bytes
                kw['crc_ok'] = crc16(data_bytes) == crc_received

        self.packets.append(UsbPacket(**kw))
        self._reset_idle()


class UsbLsTransmitter:
    """Sends complete, correctly-stuffed, CRC-checked low-speed packets.

    bit_rate_error: fractional deliberate clock error, e.g. 0.015 for the
    edge of the low-speed +/-1.5% tolerance (USB 2.0 spec 7.1.11).
    """

    def __init__(self, dp, dm, clock_hz=50e6, bit_rate_error=0.0):
        self.dp = dp
        self.dm = dm
        self.cycles_per_bit = clock_hz / (1.5e6 * (1 + bit_rate_error))

        self._queue = deque()
        self._cur_symbol = 'J'
        self._phase_start = 0.0
        self._active = False

    def send_token(self, pid_name, addr, endp):
        payload = [(addr >> i) & 1 for i in range(7)] + [(endp >> i) & 1 for i in range(4)]
        crc_bits = [(crc5(payload) >> i) & 1 for i in range(5)]
        self._enqueue_packet(self._pid_bits(pid_name) + payload + crc_bits)

    def send_sof(self, frame_num):
        payload = [(frame_num >> i) & 1 for i in range(11)]
        crc_bits = [(crc5(payload) >> i) & 1 for i in range(5)]
        self._enqueue_packet(self._pid_bits('SOF') + payload + crc_bits)

    def send_data(self, pid_name, data_bytes):
        data_bytes = bytes(data_bytes)
        payload_bits = []
        for byte in data_bytes:
            payload_bits.extend((byte >> i) & 1 for i in range(8))
        crc = crc16(data_bytes)
        crc_bits = [(crc >> i) & 1 for i in range(16)]
        self._enqueue_packet(self._pid_bits(pid_name) + payload_bits + crc_bits)

    def send_handshake(self, pid_name):
        self._enqueue_packet(self._pid_bits(pid_name))

    def _pid_bits(self, name):
        val = _PID_VALUES[name]
        check = (~val) & 0xF
        return [(val >> i) & 1 for i in range(4)] + [(check >> i) & 1 for i in range(4)]

    def _enqueue_packet(self, post_sync_bits):
        stuffed = []
        ones = 0
        for b in post_sync_bits:
            stuffed.append(b)
            if b == 1:
                ones += 1
                if ones == 6:
                    stuffed.append(0)
                    ones = 0
            else:
                ones = 0

        symbols = self._nrzi_encode(_SYNC_BITS + stuffed)
        symbols += ['SE0', 'SE0', 'J']
        self._queue.extend(symbols)

    def _nrzi_encode(self, bits):
        symbols = []
        cur = 'J'
        for b in bits:
            if b == 0:
                cur = 'K' if cur == 'J' else 'J'
            symbols.append(cur)
        return symbols

    def step(self, cycle, nets):
        if not self._active:
            if not self._queue:
                self._cur_symbol = 'J'
            else:
                self._cur_symbol = self._queue.popleft()
                self._phase_start = cycle
                self._active = True
        elif cycle >= self._phase_start + self.cycles_per_bit:
            self._phase_start += self.cycles_per_bit
            if self._queue:
                self._cur_symbol = self._queue.popleft()
            else:
                self._active = False
                self._cur_symbol = 'J'

        dp, dm = _SYMBOL_PINS[self._cur_symbol]
        return {self.dp: dp, self.dm: dm}
