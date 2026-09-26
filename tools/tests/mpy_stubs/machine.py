# Minimal stand-ins for the rp2 port's machine module (unix-port checks only).
log = []


class Pin:
    OUT, IN, PULL_UP = 1, 0, 2

    def __init__(self, n, mode=None, pull=None, value=None):
        self.n = n
        log.append(("pin", n, mode, pull, value))

    def __call__(self, v):
        log.append(("cs", self.n, v))


class SPI:
    MSB = 0

    def __init__(self, bus, **kw):
        log.append(("spi", bus, kw["baudrate"], kw["sck"].n, kw["mosi"].n, kw["miso"].n))

    def write_readinto(self, tx, rx):
        log.append(("xfer", bytes(tx)))
        for i in range(len(rx)):
            rx[i] = 0
