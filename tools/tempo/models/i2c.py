"""I2C target and passive bus monitor: open-drain, ACK/NACK, timing checks.

Both nets are open-drain: devices here only ever drive 0 or None, and the
board's pull-up resolves an undriven net to 1.

Timing minimums are from NXP UM10204 (I2C-bus specification and user
manual, Rev. 7.0, 2021) section 6.1, Table 11: Standard-mode (100 kHz),
Fast-mode (400 kHz) and Fast-mode Plus (1 MHz) columns (see docs/specs.md). Values are in seconds here and converted to whole cycles with the
device's clock_hz at construction time.
"""

# NXP UM10204 Rev. 7.0, Table 11 "Characteristics of the SDA and SCL bus
# lines for Standard, Fast, and Fast-mode Plus I2C-bus devices" (it was
# Table 10 in Rev. 6).
_TIMING = {
    'standard': {
        't_hd_sta': 4.0e-6,
        't_low': 4.7e-6,
        't_high': 4.0e-6,
        't_su_sta': 4.7e-6,
        't_su_dat': 250e-9,
        't_su_sto': 4.0e-6,
        't_buf': 4.7e-6,
    },
    'fast': {
        't_hd_sta': 0.6e-6,
        't_low': 1.3e-6,
        't_high': 0.6e-6,
        't_su_sta': 0.6e-6,
        't_su_dat': 100e-9,
        't_su_sto': 0.6e-6,
        't_buf': 1.3e-6,
    },
    'fastplus': {
        't_hd_sta': 0.26e-6,
        't_low': 0.5e-6,
        't_high': 0.26e-6,
        't_su_sta': 0.26e-6,
        't_su_dat': 50e-9,
        't_su_sto': 0.26e-6,
        't_buf': 0.5e-6,
    },
}


class I2cViolation:
    __slots__ = ("cycle", "kind", "detail")

    def __init__(self, cycle, kind, detail=""):
        self.cycle = cycle
        self.kind = kind
        self.detail = detail

    def __repr__(self):
        return f"I2cViolation(cycle={self.cycle}, kind={self.kind!r}, detail={self.detail!r})"


class I2cEvent:
    """One entry in the transaction log: a start/stop/byte/ack event."""
    __slots__ = ("cycle", "kind", "detail")

    def __init__(self, cycle, kind, detail=None):
        self.cycle = cycle
        self.kind = kind
        self.detail = detail

    def __repr__(self):
        return f"I2cEvent(cycle={self.cycle}, kind={self.kind!r}, detail={self.detail!r})"


class _I2cBusBase:
    """Shared bus-protocol engine: start/stop detection, bit sampling,
    timing checks and transaction logging. Subclasses decide what (if
    anything) to drive back onto the bus.
    """

    def __init__(self, scl, sda, mode='standard', clock_hz=50e6):
        self.scl = scl
        self.sda = sda
        self.clock_hz = clock_hz
        table = _TIMING[mode]
        self._t = {k: max(1, round(v * clock_hz)) for k, v in table.items()}

        self.events = []
        self.violations = []

        self._prev_scl = 1
        self._prev_sda = 1
        self._active = False  # between START and STOP

        self._phase = 'idle'  # 'bits' | 'ack'
        self._bit_idx = 0
        self._shift = 0
        self._ack_stage = None  # None | 'armed' | 'driving'
        self._ack_is_address = False
        self._just_addressed = False

        self._last_scl_rising = None
        self._last_scl_falling = None
        self._last_sda_change = None
        self._start_cycle = None
        self._pending_hd_sta_check = False
        self._last_stop_cycle = None
        self._sda_changes_this_high = 0

    # -- override points for an active participant -----------------
    def _drive(self, cycle):
        return {}

    def _on_byte_complete(self, cycle, byte):
        pass

    def _ack_decision(self, cycle):
        """Return True to drive an ACK (0) during the upcoming ack slot."""
        return False

    def _on_ack_sampled(self, cycle, ack_bit, is_address_ack):
        pass

    # -- engine -------------------------------------------------------
    def step(self, cycle, nets):
        cur_scl = nets.get(self.scl, 1)
        cur_sda = nets.get(self.sda, 1)

        scl_rising = cur_scl == 1 and self._prev_scl == 0
        scl_falling = cur_scl == 0 and self._prev_scl == 1
        sda_changed = cur_sda != self._prev_sda

        if cur_scl == 1 and self._prev_scl == 1 and sda_changed:
            self._sda_changes_this_high += 1
            if self._sda_changes_this_high > 1:
                self.violations.append(
                    I2cViolation(cycle, 'sda_glitch', 'SDA changed more than once while SCL high'))
            elif cur_sda == 0:
                self._on_start(cycle)
            else:
                self._on_stop(cycle)
        if scl_rising:
            self._sda_changes_this_high = 0

        if scl_rising:
            if self._last_scl_falling is not None:
                low = cycle - self._last_scl_falling
                if low < self._t['t_low']:
                    self.violations.append(I2cViolation(cycle, 'scl_low_short', f'{low} cycles'))
            if self._active and self._last_sda_change is not None:
                setup = cycle - self._last_sda_change
                if setup < self._t['t_su_dat']:
                    self.violations.append(I2cViolation(cycle, 'data_setup', f'{setup} cycles'))
            self._last_scl_rising = cycle
            if self._active:
                self._on_scl_rising(cycle, cur_sda)

        if scl_falling:
            if self._last_scl_rising is not None:
                high = cycle - self._last_scl_rising
                if high < self._t['t_high']:
                    self.violations.append(I2cViolation(cycle, 'scl_high_short', f'{high} cycles'))
            if self._pending_hd_sta_check:
                hd = cycle - self._start_cycle
                if hd < self._t['t_hd_sta']:
                    self.violations.append(I2cViolation(cycle, 'hd_sta_short', f'{hd} cycles'))
                self._pending_hd_sta_check = False
            self._last_scl_falling = cycle
            if self._active:
                self._on_scl_falling(cycle)

        if sda_changed:
            self._last_sda_change = cycle

        self._prev_scl = cur_scl
        self._prev_sda = cur_sda

        return self._drive(cycle)

    def _on_start(self, cycle):
        if self._last_scl_rising is not None:
            su = cycle - self._last_scl_rising
            if su < self._t['t_su_sta']:
                self.violations.append(I2cViolation(cycle, 'su_sta_short', f'{su} cycles'))
        if not self._active and self._last_stop_cycle is not None:
            buf = cycle - self._last_stop_cycle
            if buf < self._t['t_buf']:
                self.violations.append(I2cViolation(cycle, 'buf_short', f'{buf} cycles'))
        kind = 'repeated_start' if self._active else 'start'
        self.events.append(I2cEvent(cycle, kind))
        self._active = True
        self._phase = 'bits'
        self._bit_idx = 0
        self._shift = 0
        self._start_cycle = cycle
        self._pending_hd_sta_check = True
        self._on_bus_start(cycle)

    def _on_stop(self, cycle):
        self._matched10 = False
        if self._last_scl_rising is not None:
            su = cycle - self._last_scl_rising
            if su < self._t['t_su_sto']:
                self.violations.append(I2cViolation(cycle, 'su_sto_short', f'{su} cycles'))
        self.events.append(I2cEvent(cycle, 'stop'))
        self._active = False
        self._phase = 'idle'
        self._last_stop_cycle = cycle

    def _on_scl_rising(self, cycle, cur_sda):
        if self._phase == 'bits':
            self._shift = (self._shift << 1) | cur_sda
            self._bit_idx += 1
            if self._bit_idx == 8:
                byte = self._shift & 0xFF
                self._just_addressed = False
                self._on_byte_complete(cycle, byte)
                self._phase = 'ack'
                # Remember whether this ack is for the address byte itself
                # (decided by _on_byte_complete via _just_addressed) before
                # _on_byte_complete's role change for the *next* phase can
                # be mistaken for what this ack is about.
                self._ack_is_address = self._just_addressed
                self._ack_stage = 'armed' if self._ack_decision(cycle) else None
        elif self._phase == 'ack':
            self._on_ack_sampled(cycle, cur_sda, self._ack_is_address)
            self._phase = 'bits'
            self._bit_idx = 0
            self._shift = 0

    def _on_scl_falling(self, cycle):
        stage = getattr(self, '_ack_stage', None)
        if stage == 'armed':
            self._ack_stage = 'driving'
        elif stage == 'driving':
            self._ack_stage = None
        self._on_bus_scl_falling(cycle)

    # hooks subclasses may extend without re-deriving the whole engine
    def _on_bus_start(self, cycle):
        pass

    def _on_bus_scl_falling(self, cycle):
        pass


class I2cBusMonitor(_I2cBusBase):
    """Passive observer: never drives, only checks rules and logs
    START/STOP/address/data/ack events."""

    def __init__(self, scl, sda, mode='standard', clock_hz=50e6):
        super().__init__(scl, sda, mode=mode, clock_hz=clock_hz)
        self._expect_addr = True

    def _on_bus_start(self, cycle):
        self._expect_addr = True

    def _on_byte_complete(self, cycle, byte):
        if self._expect_addr:
            addr = byte >> 1
            rw = byte & 1
            self.events.append(I2cEvent(cycle, 'address', {'addr': addr, 'rw': rw}))
            self._expect_addr = False
        else:
            self.events.append(I2cEvent(cycle, 'data', byte))

    def _on_ack_sampled(self, cycle, ack_bit, is_address_ack):
        self.events.append(I2cEvent(cycle, 'ack' if ack_bit == 0 else 'nack'))


class I2cTarget(_I2cBusBase):
    """A single-address I2C target with an EEPROM/sensor-like register
    file: first data byte after a write-address sets the register
    pointer, subsequent bytes write regs[pointer] (auto-incrementing);
    a read (direct or after a repeated START) streams from regs[pointer].

    nack_addresses: addresses this target should NACK even if they equal
        `address` (simulates e.g. a busy device).
    nack_after: number of write-data bytes to ACK before NACKing the next
        one (None disables).
    stretch_cycles: after every SCL falling edge while a transaction is
        active, and after the address ACK, hold SCL low ourselves for
        this many extra cycles before releasing it.
    """

    def __init__(self, scl, sda, address, regs=None, mode='standard', clock_hz=50e6,
                 stretch_cycles=0, nack_addresses=(), nack_after=None, addr10=False):
        super().__init__(scl, sda, mode=mode, clock_hz=clock_hz)
        self.address = address
        self.addr10 = addr10  # UM10204 3.1.11 10-bit addressing
        self._matched10 = False
        self.regs = regs if regs is not None else bytearray(256)
        self.stretch_cycles = stretch_cycles
        self.nack_addresses = set(nack_addresses)
        self.nack_after = nack_after

        self.pointer = 0
        self._role = None  # 'reg_ptr' | 'write_data' | 'read_data' | None (unaddressed)
        self._write_count = 0
        self._write_data_ack = True
        self._will_ack_addr = False
        self._just_addressed = False
        self._out_bits = [0] * 8
        self._present_idx = 0
        self._reading_bits = False
        self._stretch_until = -1

    def _on_bus_start(self, cycle):
        self._role = 'expect_addr'

    def _on_byte_complete(self, cycle, byte):
        self._just_addressed = False

        if self.addr10 and self._role == 'expect_addr' and byte >> 3 == 0b11110:
            hi, rw = (byte >> 1) & 3, byte & 1
            self._just_addressed = True
            if rw == 0:
                self._will_ack_addr = hi == self.address >> 8
                self._role = 'addr10_lo' if self._will_ack_addr else None
                self._matched10 = False
            else:
                # read: Sr + first byte with R, only if both bytes matched before
                self._will_ack_addr = self._matched10 and hi == self.address >> 8
                self._role = 'read_data' if self._will_ack_addr else None
                if self._will_ack_addr:
                    self._load_out_byte()
            self.events.append(I2cEvent(cycle, 'address10_hi', {'hi': hi, 'rw': rw, 'ack': self._will_ack_addr}))
            return
        if self._role == 'addr10_lo':
            self._just_addressed = True
            self._will_ack_addr = byte == (self.address & 0xFF)
            self._matched10 = self._will_ack_addr
            self._role = 'reg_ptr' if self._will_ack_addr else None
            self.events.append(I2cEvent(cycle, 'address10_lo', {'lo': byte, 'ack': self._will_ack_addr}))
            return

        if self._role == 'expect_addr':
            if self.addr10:
                self._role = None
                return
            addr = byte >> 1
            rw = byte & 1
            match = addr == self.address
            self._will_ack_addr = match and addr not in self.nack_addresses
            self._just_addressed = True
            self.events.append(I2cEvent(cycle, 'address', {'addr': addr, 'rw': rw, 'ack': self._will_ack_addr}))
            if not self._will_ack_addr:
                self._role = None
                return
            if rw == 1:
                self._role = 'read_data'
                self._load_out_byte()
            else:
                self._role = 'reg_ptr'
            return

        if self._role == 'reg_ptr':
            self.pointer = byte % len(self.regs)
            self._role = 'write_data'
            self._write_count = 0
            self._write_data_ack = True
            self.events.append(I2cEvent(cycle, 'reg_ptr', byte))
        elif self._role == 'write_data':
            # Decide accept/ack together: a NACKed byte must not be
            # committed to regs (that's the point of NACKing it), so the
            # accept decision has to be made before writing, not after.
            accept = self.nack_after is None or self._write_count < self.nack_after
            self._write_data_ack = accept
            if accept:
                self.regs[self.pointer] = byte
                self.pointer = (self.pointer + 1) % len(self.regs)
                self._write_count += 1
                self.events.append(I2cEvent(cycle, 'data_write', byte))
            else:
                self.events.append(I2cEvent(cycle, 'data_write_rejected', byte))

    def _ack_decision(self, cycle):
        # Address byte's ack was already decided (match + not deliberately
        # NACKed) by _on_byte_complete; everything else depends on the
        # role _on_byte_complete just transitioned into.
        if self._just_addressed:
            return self._will_ack_addr
        if self._role == 'read_data':
            return False  # target drives data, master drives the ack/nack
        if self._role == 'reg_ptr':
            return True
        if self._role == 'write_data':
            return self._write_data_ack
        return False

    def _on_ack_sampled(self, cycle, ack_bit, is_address_ack):
        if self._role == 'read_data' and not is_address_ack:
            # A real master ack/nack for a data byte we just streamed out
            # (the address-read's own ack is target-driven and handled by
            # the else branch below, never here).
            self.events.append(I2cEvent(cycle, 'master_ack' if ack_bit == 0 else 'master_nack'))
            if ack_bit == 0:
                self._load_out_byte()
            else:
                self._role = None  # master ended the read
        else:
            self.events.append(I2cEvent(cycle, 'ack' if ack_bit == 0 else 'nack'))

    def _load_out_byte(self):
        self._out_bits = [(self.regs[self.pointer] >> i) & 1 for i in range(7, -1, -1)]
        self.pointer = (self.pointer + 1) % len(self.regs)

    def _on_bus_scl_falling(self, cycle):
        if self._role == 'read_data':
            if self._phase == 'bits':
                # SDA must hold the current bit stable through the whole
                # SCL high phase, so the next bit can only be prepared
                # once SCL has actually gone low again. _bit_idx (the
                # generic engine's rising-edge sample counter) is 0 right
                # after the preceding ack and increments at each bit's
                # sampling edge; sampling it here, on the falling edge,
                # gives the index one bit-window later than the bit_idx
                # update itself, which is exactly the lag needed: the
                # output changes only after the bit it was holding has
                # had its whole high phase to be sampled.
                self._present_idx = self._bit_idx
                self._reading_bits = True
            else:
                # phase == 'ack': this falling edge ends the 8th data
                # bit's own high phase (phase already flipped to 'ack' at
                # that bit's rising edge, mid-pulse); only now, with SCL
                # actually low, is it safe to release SDA for the
                # master's ack/nack slot.
                self._reading_bits = False
        if self.stretch_cycles:
            self._stretch_until = cycle + self.stretch_cycles

    def _drive(self, cycle):
        drive = {}
        if cycle < self._stretch_until:
            drive[self.scl] = 0

        stage = self._ack_stage
        if stage == 'driving':
            drive[self.sda] = 0
        elif self._role == 'read_data' and self._reading_bits:
            bit = self._out_bits[self._present_idx]
            if bit == 0:
                drive[self.sda] = 0
        return drive


class Eeprom24Target(I2cTarget):
    """24Cxx serial EEPROM (Microchip AT24C02C / 24LC256 behaviour, see
    docs/specs.md): 1 or 2 memory address bytes, block-select bits in the
    device address for 24C04/08/16, page writes that roll over inside the
    page, and a self-timed write cycle after STOP during which the part
    NACKs its address (ACK polling). `t_wr` is in seconds; tests shorten
    it to keep simulations fast."""

    def __init__(self, scl, sda, size=256, page=8, abytes=1, address=0x50, t_wr=5e-3,
                 mode='standard', clock_hz=50e6, stretch_cycles=0):
        super().__init__(scl, sda, address, regs=bytearray([0xFF]) * size, mode=mode,
                         clock_hz=clock_hz, stretch_cycles=stretch_cycles)
        self.size, self.page, self.abytes = size, page, abytes
        self.t_wr_cycles = max(1, round(t_wr * clock_hz))
        self.busy_until = -1
        self._blocks = max(1, size // 256) if abytes == 1 else 1
        self._pending = []  # (address, byte) written at STOP
        self._addr_left = 0
        self.writes = 0  # completed write cycles

    def _on_bus_start(self, cycle):
        self._role = 'expect_addr'

    def _on_byte_complete(self, cycle, byte):
        self._just_addressed = False
        if self._role == 'expect_addr':
            dev = byte >> 1
            rw = byte & 1
            base = self.address & ~(self._blocks - 1)
            match = (dev & ~(self._blocks - 1)) == base
            busy = cycle < self.busy_until
            self._will_ack_addr = match and not busy
            self._just_addressed = True
            self.events.append(I2cEvent(cycle, 'address', {'addr': dev, 'rw': rw, 'ack': self._will_ack_addr}))
            if not self._will_ack_addr:
                self._role = None
                return
            blk = (dev & (self._blocks - 1)) << 8
            if rw:
                if self.abytes == 1:
                    self.pointer = blk | (self.pointer & 0xFF)
                self._role = 'read_data'
                self._load_out_byte()
            else:
                self._role = 'reg_ptr'
                self._blk = blk
                self._addr_left = self.abytes
                self._new_ptr = 0
            return
        if self._role == 'reg_ptr':
            self._new_ptr = (self._new_ptr << 8) | byte
            self._addr_left -= 1
            if self._addr_left == 0:
                self.pointer = (self._blk | self._new_ptr) % self.size
                self._role = 'write_data'
                self._pending = []
            return
        if self._role == 'write_data':
            self._pending.append((self.pointer, byte))
            # roll over inside the page
            pg = self.pointer - self.pointer % self.page
            self.pointer = pg + (self.pointer + 1 - pg) % self.page

    def _ack_decision(self, cycle):
        if self._just_addressed:
            return self._will_ack_addr
        return self._role in ('reg_ptr', 'write_data')

    def _load_out_byte(self):
        self._out_bits = [(self.regs[self.pointer] >> i) & 1 for i in range(7, -1, -1)]
        self.pointer = (self.pointer + 1) % self.size

    def _on_stop(self, cycle):
        super()._on_stop(cycle)
        if self._role == 'write_data' and self._pending:
            for a, b in self._pending:
                self.regs[a] = b
            self._pending = []
            self.busy_until = cycle + self.t_wr_cycles
            self.writes += 1
        self._role = None


class StuckSda:
    """A target that lost sync and holds SDA low until it has seen
    `release_after` SCL falling edges (the situation UM10204 bus clear
    addresses)."""

    def __init__(self, scl, sda, release_after=5):
        self.scl, self.sda = scl, sda
        self.left = release_after
        self._prev = 1

    def step(self, cycle, nets):
        s = nets.get(self.scl, 1)
        if self._prev == 1 and s == 0 and self.left:
            self.left -= 1
        self._prev = s
        return {self.sda: 0} if self.left else {}
