"""Independent wire checker for the demo's Manchester convention.

MSB first; a one is high/low and a zero is low/high, each half a bit
period. The first bit is one, so its rising boundary anchors the frame.
This checks every observed wire cycle, not lane commands or registers.
It does not model an Ethernet/USB PHY or electrical behavior.
"""

from ..board import Device


class ManchesterFrame(Device):
    def __init__(self, net, payload, bit_cycles=50, idle_cycles=150):
        self.net = net
        self.payload = bytes(payload)
        if not self.payload or not self.payload[0] & 0x80:
            raise ValueError("frame must begin with a one")
        if bit_cycles < 2 or bit_cycles % 2:
            raise ValueError("bit period must have two equal integer halves")
        self.bit_cycles = bit_cycles
        self.idle_cycles = idle_cycles
        self.checked_idle_cycles = 0
        self.start_cycle = None
        self.checked_cycles = 0
        self.done = False
        self.errors = []

    def step(self, cycle, nets):
        level = nets.get(self.net, 0)
        if self.start_cycle is None:
            if level == 1:
                self.start_cycle = cycle
            else:
                return {}
        offset = cycle - self.start_cycle
        index = offset // self.bit_cycles
        if index >= len(self.payload) * 8:
            self.done = True
            if offset < len(self.payload) * 8 * self.bit_cycles + self.idle_cycles:
                self.checked_idle_cycles += 1
                if level != 0:
                    self.errors.append((cycle, index, 0, level))
            return {}
        bit = (self.payload[index // 8] >> (7 - index % 8)) & 1
        second = offset % self.bit_cycles >= self.bit_cycles // 2
        expected = bit ^ int(second)
        if level != expected:
            self.errors.append((cycle, index, expected, level))
        self.checked_cycles += 1
        return {}


class ManchesterStream(Device):
    """Check repeated frames, recording their actual wire intervals."""

    def __init__(self, net, payload, bit_cycles=50):
        self.net, self.payload, self.bit_cycles = net, bytes(payload), bit_cycles
        self.frames = []
        self.current = self._new_frame()

    def _new_frame(self):
        return ManchesterFrame(self.net, self.payload, self.bit_cycles, idle_cycles=0)

    @property
    def errors(self):
        return [e for frame in self.frames + [self.current] for e in frame.errors]

    def step(self, cycle, nets):
        self.current.step(cycle, nets)
        if self.current.done:
            self.frames.append(self.current)
            self.current = self._new_frame()
        return {}
