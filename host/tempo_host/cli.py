# SPDX-License-Identifier: Apache-2.0
"""Command line for the Tempo host library.

The same module runs on the demo board's RP2350 (MicroPython, commands typed
over USB serial) and on a PC (CPython, `tools/tempo_cli.py`, against the
board or against the chip model). Commands: see HELP. Pins are Tempo pin
indices (0-7 = uio0-7, 8-15 = ui0-7 as inputs) or names: uio4, ui3.
Numbers accept 0x prefixes. Output goes through `out(line)`.
"""

from .link import TempoError

# MicroPython keeps no module docstrings, so the help text is a constant.
HELP = """\
i2c scan [sda=4] [scl=5] [mode=sm|fm|fmp]   flash id | flash sfdp | flash read ADDR N [raw=1]
i2c dump ADDR [n=256] [reg=0]              jtag idcode [TCK TMS TDI TDO]
i2c recover                                jtag find [PINS...]
eeprom read MEM N [type=24c02]             swd attach [swdio=0] [swclk=1]
uart autobaud [rx=1]                       la capture PINS... [us=10000] [n=160] [trig=PIN:fall] [vcd=1]
trigger uart RX OUT BAUD MATCH DELAY WIDTH [n=1]
trigger edge PIN OUT COUNT DELAY WIDTH [n=1]
status | help
Pins: Tempo pin indices or names (uio0-7, ui0-7). Flash, SWD, JTAG pins: key=value."""


def pin(tok):
    t = str(tok)
    if t.startswith("uio"):
        return int(t[3:])
    if t.startswith("ui"):
        return 8 + int(t[2:])
    return int(t, 0)


def num(tok):
    return int(str(tok), 0)


def hexdump(data, base=0):
    lines = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        h = " ".join("%02x" % b for b in row)
        a = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append("%06x  %-47s  %s" % (base + i, h, a))
    return lines


def split(line):
    pos, kw = [], {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            kw[k] = v
        else:
            pos.append(tok)
    return pos, kw


class Cli:
    def __init__(self, link, out=print):
        self.link = link
        self.out = out
        self.spi = None

    def run(self, line):
        """Run one command line. Errors from the chip or the target are
        printed as `error: ...` and returned as False."""
        pos, kw = split(line.strip())
        if not pos:
            return True
        name = pos[0] + ("_" + pos[1] if len(pos) > 1 and hasattr(self, "c_%s_%s" % (pos[0], pos[1])) else "")
        fn = getattr(self, "c_" + name, None)
        if fn is None:
            self.out("error: unknown command %r (try help)" % line.strip())
            return False
        args = pos[2:] if "_" in name else pos[1:]
        try:
            fn(args, kw)
        except (TempoError, ValueError, IndexError) as e:
            self.out("error: %s" % e)
            return False
        return True

    # ---- general
    def c_help(self, a, kw):
        for ln in HELP.split("\n"):
            self.out(ln.strip("\n"))

    def c_status(self, a, kw):
        st = self.link.status()
        self.out(repr(st))
        for e in st.errors():
            self.out("  " + e)

    # ---- I2C
    def _i2c(self, kw):
        from .i2c import I2c
        return I2c(self.link, sda=pin(kw.get("sda", 4)), scl=pin(kw.get("scl", 5)), mode=kw.get("mode", "fm"))

    def c_i2c_scan(self, a, kw):
        found = self._i2c(kw).scan()
        self.out("     " + " ".join("%2x" % c for c in range(16)))
        for row in range(0, 128, 16):
            cells = []
            for a7 in range(row, row + 16):
                cells.append("%02x" % a7 if a7 in found else ("  " if a7 < 8 or a7 > 0x77 else "--"))
            self.out("%02x: %s" % (row, " ".join(cells)))
        self.out("found: " + (" ".join("0x%02x" % f for f in found) or "none"))

    def c_i2c_dump(self, a, kw):
        addr = num(a[0])
        n = num(kw.get("n", 256))
        reg = num(kw.get("reg", 0))
        data = self._i2c(kw).read_regs(addr, reg, n)
        for ln in hexdump(data, reg):
            self.out(ln)

    def c_i2c_recover(self, a, kw):
        n = self._i2c(kw).recover()
        self.out("SDA released after %d SCL pulses" % n)

    def c_eeprom_read(self, a, kw):
        from .i2c import Eeprom24
        rom = Eeprom24(self._i2c(kw), kw.get("type", "24c02"))
        mem, n = num(a[0]), num(a[1])
        for ln in hexdump(rom.read(mem, n), mem):
            self.out(ln)

    # ---- SPI flash
    def _flash(self, kw):
        from .spi import Spi, SpiFlash
        s = Spi(self.link, mosi=pin(kw.get("mosi", 0)), miso=pin(kw.get("miso", 1)),
                sck=pin(kw.get("sck", 2)), cs=pin(kw.get("cs", 3)), mode=num(kw.get("mode", 0)),
                period=num(kw.get("period", 8)))
        return SpiFlash(s)

    def c_flash_id(self, a, kw):
        m, t, c = self._flash(kw).jedec_id()
        from . import jep106
        self.out("JEDEC ID %02x %02x %02x: %s, capacity code 0x%02x (%d bytes if 2^n)"
                 % (m, t, c, jep106.name(m & 0x7F), c, 1 << c if c < 32 else 0))

    def c_flash_sfdp(self, a, kw):
        info = self._flash(kw).sfdp_info()
        if info is None:
            self.out("no SFDP signature")
        else:
            self.out("SFDP rev %d.%d, %d bytes, 4 KB erase opcode 0x%02x"
                     % (info["rev"][0], info["rev"][1], info["size"], info["erase_4k_opcode"]))

    def c_flash_read(self, a, kw):
        addr, n = num(a[0]), num(a[1])
        f = self._flash(kw)
        t0 = self.link.t.ticks_us()
        data = f.read(addr, n)
        dt = self.link.t.ticks_us() - t0
        if kw.get("raw"):
            self.out("raw " + "".join("%02x" % b for b in data))
        else:
            for ln in hexdump(data, addr):
                self.out(ln)
        self.out("%d bytes in %d us" % (n, dt))

    # ---- JTAG / SWD
    def c_jtag_idcode(self, a, kw):
        from .jtag import Jtag
        p = [pin(x) for x in a] if a else [0, 1, 2, 3]
        j = Jtag(self.link, *p, period=num(kw.get("period", 20)))
        ids = j.idcodes()
        self.out("%d device(s), nearest TDO first" % len(ids))
        for k, v in enumerate(ids):
            self.out("  %d: %s" % (k, Jtag.describe(v)))

    def c_jtag_find(self, a, kw):
        from .jtag import find_pins, Jtag
        cands = tuple(pin(x) for x in a) if a else (0, 1, 2, 3, 4, 5, 6, 7)
        r = find_pins(self.link, cands)
        if r is None:
            self.out("no valid IDCODE on any pin assignment")
            return
        self.out("TCK=%s TMS=%s TDI=%s TDO=%s: %s" % (r["tck"], r["tms"], r["tdi"], r["tdo"],
                                                      Jtag.describe(r["idcode"])))

    def c_swd_attach(self, a, kw):
        from .swd import Swd
        s = Swd(self.link, swdio=pin(kw.get("swdio", 0)), swclk=pin(kw.get("swclk", 1)),
                period=num(kw.get("period", 20)))
        dp = s.connect()
        self.out("DPIDR 0x%08x: %s, DPv%d, part 0x%02x, rev %d"
                 % (dp["dpidr"], dp["designer"], dp["version"], dp["partno"], dp["revision"]))
        s.power_up()
        rt = s.rom_table()
        if rt["base"] is None:
            self.out("MEM-AP IDR 0x%08x, no ROM table" % rt["idr"])
            return
        self.out("MEM-AP IDR 0x%08x, ROM table at 0x%08x" % (rt["idr"], rt["base"] & 0xFFFFF000))
        for e in rt["entries"]:
            self.out("  component 0x%08x class 0x%x" % (e["address"], e["class"]))

    # ---- UART
    def c_uart_autobaud(self, a, kw):
        from .uart import Autobaud, snap_baud
        measured, w = Autobaud(self.link, rx=pin(kw.get("rx", a[0] if a else 1))).measure()
        self.out("measured %d baud, nearest standard rate %d (%d pulse widths)"
                 % (int(measured), snap_baud(measured), len(w)))

    # ---- logic analyser and trigger
    def c_la_capture(self, a, kw):
        from .la import LogicAnalyser
        pins = [pin(x) for x in a] or [0]
        trig = None
        if "trig" in kw:
            tp, _, edge = kw["trig"].partition(":")
            trig = (pin(tp), edge or "fall")
        la = LogicAnalyser(self.link, pins=pins, trigger=trig, n=num(kw.get("n", 160)))
        cap = la.capture(timeout_us=num(kw.get("us", 10000)))
        for k, p in enumerate(pins):
            e = cap.edges[k]
            line = "ch%d (pin %d): initial %d, %d edges" % (k, p, cap.initial[k], len(e))
            try:
                m = cap.measure(k)
                line += ", %.1f Hz, duty %.3f, high %s..%s low %s..%s cycles" % (
                    m["frequency_hz"], m["duty"] or 0, m["high_min"], m["high_max"], m["low_min"], m["low_max"])
            except TempoError:
                pass
            if k in cap.overruns:
                line += ", OVERRUN (edges lost)"
            self.out(line)
        if kw.get("vcd"):
            buf = _Lines()
            cap.to_vcd(buf)
            for ln in buf.lines():
                self.out(ln)

    def c_trigger_uart(self, a, kw):
        from .trigger import PulseTrigger
        rx, out, baud, match, delay, width = pin(a[0]), pin(a[1]), num(a[2]), num(a[3]), num(a[4]), num(a[5])
        t = PulseTrigger(self.link, out=out, trig=rx, delay=delay, width=width, kind="uart",
                         baud=baud, match=match)
        self._trig_wait(t, kw)

    def c_trigger_edge(self, a, kw):
        from .trigger import PulseTrigger
        p, out, count, delay, width = pin(a[0]), pin(a[1]), num(a[2]), num(a[3]), num(a[4])
        t = PulseTrigger(self.link, out=out, trig=p, delay=delay, width=width, kind="edge", count=count)
        self._trig_wait(t, kw)

    def _trig_wait(self, t, kw):
        n = num(kw.get("n", 1))
        got = t.wait(n, timeout_us=num(kw.get("us", 1000000)))
        self.out("%d pulse(s) generated" % got)


class _Lines:
    """Minimal file-like sink (MicroPython has no io.StringIO.getvalue on
    every port)."""

    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    def lines(self):
        return "".join(self.parts).rstrip("\n").split("\n")
