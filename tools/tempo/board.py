"""Cycle-level board model: chip pads, external devices, pull-ups.

Every net is resolved once per clock cycle. A driver is a value 0 or 1 or
None (not driving). Open-drain devices only ever drive 0 or None. If any
driver drives 0 and another drives 1 at the same time the net is in
contention; the board records it and resolves to 0.
"""

from dataclasses import dataclass, field
from .clock import CLOCK_HZ


class Device:
    """Base class for external protocol models attached to board nets."""

    def step(self, cycle: int, nets: dict) -> dict:
        """Called once per cycle with the resolved net levels from the
        previous resolution. Returns {net_name: 0 | 1 | None} drives that
        take effect in this cycle's resolution."""
        return {}


@dataclass
class Board:
    clock_hz: float = CLOCK_HZ
    pullups: set = field(default_factory=set)
    devices: list = field(default_factory=list)
    nets: dict = field(default_factory=dict)
    contention: list = field(default_factory=list)
    cycle: int = 0

    def attach(self, dev: Device) -> Device:
        self.devices.append(dev)
        return dev

    def cycles(self, seconds: float) -> int:
        return round(seconds * self.clock_hz)

    def resolve(self, drives: list) -> dict:
        """drives: list of {net: level} dicts from all drivers this cycle."""
        names = set(self.nets) | self.pullups
        for d in drives:
            names |= set(d)
        out = {}
        for n in names:
            vals = [d[n] for d in drives if d.get(n) is not None]
            if 0 in vals and 1 in vals:
                self.contention.append((self.cycle, n))
                out[n] = 0
            elif vals:
                out[n] = vals[0]
            else:
                out[n] = 1 if n in self.pullups else self.nets.get(n, 0)
        return out

    def tick(self, chip_drives: dict) -> dict:
        """Advance one cycle. chip_drives: {net: level or None}."""
        drives = [chip_drives] + [dev.step(self.cycle, self.nets) for dev in self.devices]
        self.nets = self.resolve(drives)
        self.cycle += 1
        return self.nets
