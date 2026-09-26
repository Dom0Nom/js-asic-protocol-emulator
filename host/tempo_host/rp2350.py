# SPDX-License-Identifier: Apache-2.0
"""Tempo on the Tiny Tapeout demo board (ETR, RP2350B), MicroPython only.

The host port uses the RP2350's hardware SPI0 (notes/research-host.md,
section 4c; pin map from tt-micropython-firmware gpio_map_dbv3.py):

    HSCK  = ui_in[5]  = GPIO22 (SPI0 SCK)
    HMOSI = ui_in[6]  = GPIO23 (SPI0 TX)
    HMISO = uo_out[3] = GPIO36 (SPI0 RX)
    HCS_N = ui_in[7]  = GPIO24 (plain GPIO)

`machine.SPI` moves each transaction's bytes in C, so the Python cost is
per transaction, not per bit. SCK is clock / 12 at most (host port limit).

    from tempo_host.rp2350 import board
    link = board()                 # select the project, clock, reset
    from tempo_host.cli import Cli
    Cli(link).run("i2c scan")
"""

import time

from machine import Pin, SPI

from .link import Link

GPIO_SCK, GPIO_MOSI, GPIO_MISO, GPIO_CS = 22, 23, 36, 24
GPIO_UIO = (25, 26, 27, 28, 29, 30, 31, 32)  # uio[0..7]
GPIO_UI = (17, 18, 19, 20, 21, 22, 23, 24)   # ui_in[0..7]


class Rp2350Transport:
    def __init__(self, clock_hz=50000000, sck_div=12):
        """clock_hz: the project clock the board generates. The SPI clock is
        clock_hz / sck_div (sck_div >= 12, the host port's limit); the
        RP2350 rounds it down to what its prescaler can make."""
        if sck_div < 12:
            raise ValueError("HSCK must be at most clock / 12")
        self.cs = Pin(GPIO_CS, Pin.OUT, value=1)
        self.spi = SPI(0, baudrate=clock_hz // sck_div, polarity=0, phase=0, bits=8,
                       firstbit=SPI.MSB, sck=Pin(GPIO_SCK), mosi=Pin(GPIO_MOSI), miso=Pin(GPIO_MISO))
        self._last = time.ticks_us()
        self._acc = 0

    def xfer(self, data):
        rx = bytearray(len(data))
        self.cs(0)
        self.spi.write_readinto(data, rx)
        self.cs(1)
        return bytes(rx)

    def sleep_us(self, us):
        time.sleep_us(int(us))

    def ticks_us(self):
        # time.ticks_us() wraps (2^30 us on rp2); the library subtracts
        # timestamps, so hand it a monotonic count
        now = time.ticks_us()
        self._acc += time.ticks_diff(now, self._last)
        self._last = now
        return self._acc


def board(project="tt_um_dom0nom_protoemu", clock_hz=50000000, pullups=(), inputs=()):
    """Select Tempo on the demo board, start its clock, reset it, and return
    a Link. pullups: uio indices to pull up with the RP2350's internal
    pull-ups (the board has none on uio; I2C needs them, or external 2.2k-4.7k
    resistors for anything faster than a few kHz). inputs: ui_in indices
    (0-4) that an external signal drives, so the RP2350 stops driving them."""
    from ttboard.boot.demoboard_detect import DemoboardDetect
    from ttboard.demoboard import DemoBoard
    from ttboard.mode import RPMode
    DemoboardDetect.probe()
    tt = DemoBoard.get()
    getattr(tt.shuttle, project).enable()
    tt.mode = RPMode.ASIC_RP_CONTROL
    tt.ui_in.value = 0x80  # HCS_N high, everything else low
    tt.uio_oe_pico.value = 0  # Tempo owns every uio pin
    tt.clock_project_PWM(clock_hz)
    tt.reset_project(True)
    time.sleep_ms(2)
    tt.reset_project(False)
    for i in pullups:
        Pin(GPIO_UIO[i], Pin.IN, Pin.PULL_UP)
    for i in inputs:
        if i >= 5:
            raise ValueError("ui_in[5:7] carry the host port")
        Pin(GPIO_UI[i], Pin.IN, None)
    return Link(Rp2350Transport(clock_hz), clock_hz)
