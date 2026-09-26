# SPDX-License-Identifier: Apache-2.0
"""Line protocol over the RP2350's USB serial, for a PC.

Run on the board (e.g. from main.py):

    from tempo_host.rp2350 import board
    from tempo_host.serve import serve
    serve(board(pullups=(4, 5)))

Each request is one line; each reply ends with a line ".":

    <CLI command>        CLI output lines, then "."
    x <hex>              one host-port transaction: "= <hex reply>", "."
    s <us>               sleep: "."
    t                    "= <ticks_us>", "."

The `x`/`s`/`t` requests let the PC run the host library itself
(`tools/tempo_cli.py --pc`), for example to stream a flash dump into a file;
the CLI requests run everything on the board. Ctrl-C returns to the REPL.
"""

import sys
import binascii

from .cli import Cli


def handle(link, cli, line, out):
    line = line.strip()
    if line.startswith("x "):
        rx = link.t.xfer(binascii.unhexlify(line[2:]))
        out("= " + binascii.hexlify(rx).decode())
    elif line.startswith("s "):
        link.t.sleep_us(int(line[2:]))
    elif line == "t":
        out("= %d" % link.t.ticks_us())
    elif line:
        cli.run(line)
    out(".")


def serve(link, inp=None, out=print):
    inp = inp or sys.stdin
    cli = Cli(link, out)
    out("tempo ready")
    while True:
        line = inp.readline()
        if not line:
            return
        handle(link, cli, line, out)
