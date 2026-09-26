# SPDX-License-Identifier: Apache-2.0
"""Device emulation: Tempo answers as an I2C 24C02 EEPROM, so firmware on
an MCU can be tested before the real part is fitted, or against contents
and behaviour (clock stretching) that a real part never produces.

Runs the "i2ctarget" firmware image (fw/i2ctarget.s). The 256 bytes live in
the chip's SRAM; the host reads and writes them at any time.
"""

from .images import IMAGES

NOPIN = 15


class Eeprom24c02Target:
    def __init__(self, link, sda=4, scl=5, addr=0x50, contents=None, stretch=0):
        """stretch: cycles to hold SCL low after every ACK (0: never)."""
        self.link = link
        link.load(IMAGES["i2ctarget"], {
            "pins": NOPIN << 12 | scl << 8 | sda << 4 | sda,
            "sclpin": NOPIN << 12 | scl << 8 | NOPIN << 4 | NOPIN,
            "pins2": NOPIN << 12 | scl << 8 | sda << 4 | NOPIN,
            "addr": addr, "stretch": stretch,
        })
        self.base = link.param_values["mem"]
        self.load(contents if contents is not None else bytes([0xFF]) * 256)

    def load(self, data):
        """Set the emulated contents (one byte per SRAM word)."""
        self.link.write_imem(self.base, list(bytes(data)[:256]))

    def contents(self):
        return bytes(w & 0xFF for w in self.link.read_imem(self.base, 256))

    def writes(self):
        """Completed write transactions since the last call:
        (first address, byte count)."""
        return [(w & 0xFF, w >> 8) for w in self.link.get_some()]
