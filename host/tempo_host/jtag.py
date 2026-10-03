# SPDX-License-Identifier: Apache-2.0
"""JTAG: chain scan, IDCODE decode, chain length, header pin finder.

Runs the "jtag" firmware image (fw/jtag.s): lane 0 drives TCK and TDI and
samples TDO, lane 1 drives TMS on the same timeline. The host tracks the
TAP state; facts per IEEE 1149.1 as summarised in docs/specs.md section 4.
"""

from .images import IMAGES
from .link import TempoError
from . import jep106

NOPIN = 15


class JtagError(TempoError):
    pass


def _bits(v, n):
    return [(v >> i) & 1 for i in range(n)]


def _val(bits):
    return sum(b << i for i, b in enumerate(bits))


class Jtag:
    def __init__(self, link, tck=0, tms=1, tdi=2, tdo=3, period=20):
        """Pins are Tempo pin indices; tdi may be None (left undriven, for
        pin finding). period: cycles per TCK (TCK = link.clock_hz / period)."""
        self.link = link
        self.period = period
        self.load(tck, tms, tdi, tdo)

    def load(self, tck, tms, tdi, tdo):
        half = self.period // 2
        pins = {
            "pins": NOPIN << 12 | tck << 8 | tdo << 4 | (NOPIN if tdi is None else tdi),
            "tms": NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | tms,
        }
        if getattr(self, "pins", None) and self.link.image is IMAGES["jtag"]:
            self.link.patch(pins)  # re-pin without reloading the code
        else:
            pins.update({"per": self.period, "cr": half, "cf": self.period, "samp": half})
            self.link.load(IMAGES["jtag"], pins)
        self.pins = (tck, tms, tdi, tdo)
        self.state = None

    # ---- raw vectors
    def shift(self, tms, tdi):
        """Clock len(tms) bits; tms and tdi are bit lists. Returns TDO bits.
        At most two vectors are in flight, so neither mailbox can fill up
        while the other side waits."""
        out = []
        inflight = []
        for i in range(0, len(tms), 16):
            t, d = tms[i:i + 16], tdi[i:i + 16]
            self.link.put([len(t), _val(t), _val(d)])
            inflight.append(len(t))
            if len(inflight) == 2:
                out += _bits(self.link.get(1)[0], inflight.pop(0))
        for n in inflight:
            out += _bits(self.link.get(1)[0], n)
        return out

    # ---- TAP navigation (host-side state tracking)
    def reset(self):
        """Five TMS=1 clocks reach Test-Logic-Reset from any state, then
        Run-Test/Idle."""
        self.shift([1, 1, 1, 1, 1, 0], [1] * 6)
        self.state = "RTI"

    def scan_dr(self, tdi_bits):
        """From Run-Test/Idle: shift DR with tdi_bits, back to Run-Test/Idle.
        Returns the bits shifted out."""
        return self._scan([1, 0, 0], tdi_bits)

    def scan_ir(self, tdi_bits):
        return self._scan([1, 1, 0, 0], tdi_bits)

    def _scan(self, enter, tdi_bits):
        if self.state != "RTI":
            self.reset()
        n = len(tdi_bits)
        tms = enter + [0] * (n - 1) + [1] + [1, 0]
        tdi = [1] * len(enter) + list(tdi_bits) + [1, 1]
        tdo = self.shift(tms, tdi)
        return tdo[len(enter):len(enter) + n]

    # ---- chain inspection
    def idcodes(self, max_devices=16):
        """Scan the chain from Test-Logic-Reset. Each TAP contributes a
        32-bit IDCODE (first bit 1) or a 1-bit BYPASS (first bit 0), as
        OpenOCD's autoprobe reads it. Returns a list of IDCODEs, None for a
        TAP without one, nearest-TDO first."""
        self.reset()
        bits = self.scan_dr([1] * (32 * max_devices + 32))
        out = []
        i = 0
        while i < len(bits):
            if bits[i] == 0:
                out.append(None)
                i += 1
                continue
            v = _val(bits[i:i + 32])
            if v == 0xFFFFFFFF or i + 32 > len(bits):
                break  # the ones we shifted in: end of chain
            out.append(v)
            i += 32
        return out

    @staticmethod
    def describe(idcode):
        if idcode is None:
            return "no IDCODE (BYPASS)"
        ver, part, mfg = jep106.decode_idcode(idcode)
        return "0x%08x %s part 0x%04x version %d" % (idcode, mfg, part, ver)

    def chain_length(self, max_ir=64, max_devices=32):
        """Number of TAPs, via BYPASS: all-ones IR everywhere, flush DR with
        zeros, then count the clocks a single 1 takes to reach TDO."""
        self.reset()
        self.scan_ir([1] * max_ir)
        bits = self.scan_dr([0] * max_devices + [1] + [0] * max_devices)
        for k in range(max_devices + 1, len(bits)):
            if bits[k]:
                return k - max_devices
        raise JtagError("no 1 came back through the chain: open TDI/TDO?")

    def ir_length(self, max_ir=128):
        """Total IR length of the chain: fill with ones, then time a 0."""
        self.reset()
        bits = self.scan_ir([1] * max_ir + [0] + [1] * max_ir)
        for k in range(max_ir, len(bits)):
            if bits[k] == 0:
                return k - max_ir
        raise JtagError("IR length not found")


def valid_idcode(v):
    """Plausible IDCODE: bit 0 set, not all ones, manufacturer not 0x7F/0."""
    mfg = (v >> 1) & 0x7FF
    return v & 1 and v != 0xFFFFFFFF and mfg & 0x7F not in (0, 0x7F)


def find_pins(link, candidates=(0, 1, 2, 3, 4, 5, 6, 7), period=20, log=None):
    """Identify TCK, TMS, TDO and then TDI on an unlabelled header.

    For every (TCK, TMS, TDO) triple of candidate pins: reset the TAP with
    TMS high, go to Shift-DR and read 32 bits; a valid IDCODE identifies the
    three. Then each remaining pin is tried as TDI: a pattern shifted in
    must come out of TDO after the 32 IDCODE bits. Only bidirectional pins
    (uio 0-7) make sense as candidates. Driving unknown pins can fight a
    target output: use series resistors (docs/use-cases.md)."""
    j = None
    found = None
    for tck in candidates:
        for tms in candidates:
            if tms == tck:
                continue
            for tdo in candidates:
                if tdo in (tck, tms):
                    continue
                if j is None:
                    j = Jtag(link, tck, tms, None, tdo, period)
                else:
                    j.load(tck, tms, None, tdo)
                j.reset()
                v = _val(j.scan_dr([1] * 32))
                if valid_idcode(v):
                    found = (tck, tms, tdo, v)
                    break
                if log:
                    log("TCK=%d TMS=%d TDO=%d: 0x%08x" % (tck, tms, tdo, v))
            if found:
                break
        if found:
            break
    if not found:
        return None
    tck, tms, tdo, v = found
    pattern = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 1]
    for tdi in candidates:
        if tdi in (tck, tms, tdo):
            continue
        j.load(tck, tms, tdi, tdo)
        j.reset()
        # the pattern comes out after the whole chain's DR length
        out = j.scan_dr(pattern + [0] * (32 * 4))
        for k in range(1, len(out) - len(pattern) + 1):
            if out[k:k + len(pattern)] == pattern:
                return {"tck": tck, "tms": tms, "tdi": tdi, "tdo": tdo, "idcode": v}
    return {"tck": tck, "tms": tms, "tdi": None, "tdo": tdo, "idcode": v}
