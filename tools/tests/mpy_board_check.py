# Run under the MicroPython unix port with tests/mpy_stubs first on the path.
import sys
import machine
from tempo_host.rp2350 import board
from tempo_host.serve import handle
from tempo_host.cli import Cli

link = board(pullups=(4, 5))
assert ("spi", 0, 50000000 // 12, 22, 23, 36) in machine.log, machine.log
st = link.status()
assert ("xfer", bytes([7]) + bytes(9)) in machine.log
t0 = link.t.ticks_us()
link.t.sleep_us(1000)
assert link.t.ticks_us() - t0 >= 1000
out = []
handle(link, Cli(link, out.append), "x 0700\n", out.append)
assert out == ["= 0000", "."], out
print("BOARD CHECK OK")
