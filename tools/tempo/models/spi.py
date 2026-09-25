"""SPI slave models: full duplex, mode 0-3, with setup/hold/framing checks.

Convention: CS is active low (asserted = 0), matching the vast majority of
real SPI parts and the phrasing in the brief ("drives MISO, None when CS
high").

Mode -> (CPOL, CPHA):
  0 -> (0, 0)   1 -> (0, 1)   2 -> (1, 0)   3 -> (1, 1)

Leading edge = first clock transition away from idle each clock pulse
(rising if CPOL=0, falling if CPOL=1). CPHA=0 samples on the leading edge
and shifts a new output bit on the trailing edge; CPHA=1 shifts on the
leading edge and samples on the trailing edge. This is standard SPI timing
(see any SPI mode timing diagram, e.g. Motorola/Freescale SPI block guide).
"""


class SpiViolation:
    __slots__ = ("cycle", "kind", "detail")

    def __init__(self, cycle, kind, detail=""):
        self.cycle = cycle
        self.kind = kind
        self.detail = detail

    def __repr__(self):
        return f"SpiViolation(cycle={self.cycle}, kind={self.kind!r}, detail={self.detail!r})"


def _to_bits(byte, msb_first):
    bits = [(byte >> i) & 1 for i in range(8)]
    if msb_first:
        bits.reverse()
    return bits


def _from_bits(bits, msb_first):
    order = reversed(bits) if msb_first else bits
    value = 0
    for i, b in enumerate(order):
        value |= (b << i)
    return value


class SpiSlave:
    """Full-duplex SPI slave/target model with setup/hold/CS checks.

    transactions: list of (mosi_bytes, miso_bytes), one per CS-active
        session, each a bytes object.
    violations: list of SpiViolation, kinds 'setup', 'hold', 'cs_framing'.
    """

    def __init__(self, sck, mosi, miso, cs, mode=0, msb_first=True,
                 response=None, min_setup_cycles=1, min_hold_cycles=1):
        self.sck = sck
        self.mosi = mosi
        self.miso = miso
        self.cs = cs
        self.msb_first = msb_first
        self.response = response
        self.min_setup_cycles = min_setup_cycles
        self.min_hold_cycles = min_hold_cycles

        self.cpol = 0 if mode in (0, 1) else 1
        self.cpha = 0 if mode in (0, 2) else 1
        self._leading = 'rising' if self.cpol == 0 else 'falling'
        self._trailing = 'falling' if self.cpol == 0 else 'rising'
        self._sample_edge = self._leading if self.cpha == 0 else self._trailing
        self._shift_edge = self._trailing if self.cpha == 0 else self._leading

        self.transactions = []
        self.violations = []

        self._prev_sck = self.cpol
        self._prev_cs = 1
        self._prev_mosi = 0
        self._cs_active = False
        self._last_mosi_change = 0
        self._last_sample_edge = None

        self._rx_bits = []
        self._out_pos = 0
        self._tx_bits = [0] * 8
        self._tx_byte_val = 0
        self._tx_level = 0
        self._tx_pending_flush = False

        self._cur_mosi_bytes = []
        self._cur_miso_bytes = []
        self._bits_this_txn = 0

    def step(self, cycle, nets):
        cur_sck = nets.get(self.sck, self.cpol)
        cur_cs = nets.get(self.cs, 1)
        cur_mosi = nets.get(self.mosi, 0)

        if cur_cs != self._prev_cs:
            if cur_cs == 0:
                self._start_transaction(cycle)
            else:
                self._end_transaction(cycle)

        if cur_mosi != self._prev_mosi:
            if (self._cs_active and self._last_sample_edge is not None
                    and (cycle - self._last_sample_edge) < self.min_hold_cycles):
                self.violations.append(SpiViolation(cycle, 'hold', 'MOSI changed too soon after sample'))
            self._last_mosi_change = cycle

        if self._cs_active and cur_sck != self._prev_sck:
            edge = 'rising' if cur_sck == 1 else 'falling'
            if edge == self._sample_edge:
                self._on_sample(cycle, cur_mosi)
            if edge == self._shift_edge:
                self._on_shift(cycle)

        self._prev_sck = cur_sck
        self._prev_cs = cur_cs
        self._prev_mosi = cur_mosi

        level = self._tx_level if self._cs_active else None
        return {self.miso: level}

    def _start_transaction(self, cycle):
        self._cs_active = True
        self._rx_bits = []
        self._cur_mosi_bytes = []
        self._cur_miso_bytes = []
        self._bits_this_txn = 0
        self._last_mosi_change = cycle
        self._last_sample_edge = None
        self._load_next_tx_byte()
        if self.cpha == 0:
            self._tx_level = self._tx_bits[0]
            self._out_pos = 1
        else:
            self._tx_level = 0
            self._out_pos = 0

    def _end_transaction(self, cycle):
        if self._tx_pending_flush and self._out_pos >= 8:
            self._cur_miso_bytes.append(self._tx_byte_val)
            self._tx_pending_flush = False
        if self._bits_this_txn % 8 != 0:
            self.violations.append(
                SpiViolation(cycle, 'cs_framing', f'{self._bits_this_txn} bits, not a multiple of 8'))
        self.transactions.append((bytes(self._cur_mosi_bytes), bytes(self._cur_miso_bytes)))
        self._cs_active = False

    def _on_sample(self, cycle, mosi_level):
        setup = cycle - self._last_mosi_change
        if setup < self.min_setup_cycles:
            self.violations.append(SpiViolation(cycle, 'setup', f'MOSI stable only {setup} cycles before sample'))
        self._rx_bits.append(mosi_level)
        self._last_sample_edge = cycle
        self._bits_this_txn += 1
        if len(self._rx_bits) == 8:
            self._cur_mosi_bytes.append(_from_bits(self._rx_bits, self.msb_first))
            self._rx_bits = []

    def _on_shift(self, cycle):
        if self._out_pos >= 8:
            self._cur_miso_bytes.append(self._tx_byte_val)
            self._tx_pending_flush = False
            self._load_next_tx_byte()
            self._tx_level = self._tx_bits[0]
            self._out_pos = 1
        else:
            self._tx_level = self._tx_bits[self._out_pos]
            self._out_pos += 1

    def _load_next_tx_byte(self):
        idx = len(self._cur_miso_bytes)
        mosi_so_far = tuple(self._cur_mosi_bytes)
        byte = self._next_response_byte(idx, mosi_so_far) & 0xFF
        self._tx_byte_val = byte
        self._tx_bits = _to_bits(byte, self.msb_first)
        self._tx_pending_flush = True

    def _next_response_byte(self, idx, mosi_so_far):
        if callable(self.response):
            return self.response(idx, mosi_so_far)
        if self.response:
            return self.response[idx % len(self.response)]
        return 0x00


class SpiFlashLike(SpiSlave):
    """Tiny SPI-NOR-flash-like command set: 0x9F JEDEC ID, 0x03 read, 0x02 page program."""

    def __init__(self, sck, mosi, miso, cs, mem=None, jedec_id=(0xAA, 0x55, 0x01), **kw):
        kw.pop('response', None)
        super().__init__(sck, mosi, miso, cs, response=None, **kw)
        self.mem = mem if mem is not None else bytearray(256)
        self.jedec_id = bytes(jedec_id)
        self._cmd = None
        self._addr_bytes = []
        self._addr = 0
        self._read_ptr = 0

    def _start_transaction(self, cycle):
        self._cmd = None
        self._addr_bytes = []
        super()._start_transaction(cycle)

    def _next_response_byte(self, idx, mosi_so_far):
        if idx == 0 or not mosi_so_far:
            return 0x00
        cmd = mosi_so_far[0]
        if cmd == 0x9F:
            pos = idx - 1
            return self.jedec_id[pos] if pos < len(self.jedec_id) else 0x00
        if cmd == 0x03:
            if idx <= 3:
                return 0x00
            if idx == 4:
                self._read_ptr = self._addr
            val = self.mem[self._read_ptr % len(self.mem)]
            self._read_ptr += 1
            return val
        return 0x00

    def _on_shift(self, cycle):
        # need the just-completed mosi byte (if any) processed before we
        # compute the next output, which _load_next_tx_byte does via
        # _next_response_byte reading self._cur_mosi_bytes/self._addr.
        super()._on_shift(cycle)

    def _on_sample(self, cycle, mosi_level):
        had = len(self._cur_mosi_bytes)
        super()._on_sample(cycle, mosi_level)
        if len(self._cur_mosi_bytes) > had:
            self._on_mosi_byte(self._cur_mosi_bytes[-1], had)

    def _on_mosi_byte(self, byte, idx):
        if idx == 0:
            self._cmd = byte
            self._addr_bytes = []
            return
        if self._cmd in (0x02, 0x03) and idx in (1, 2, 3):
            self._addr_bytes.append(byte)
            if idx == 3:
                self._addr = (self._addr_bytes[0] << 16) | (self._addr_bytes[1] << 8) | self._addr_bytes[2]
            return
        if self._cmd == 0x02 and idx >= 4:
            self.mem[self._addr % len(self.mem)] = byte
            self._addr += 1
