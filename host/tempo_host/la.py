# SPDX-License-Identifier: Apache-2.0
"""Logic analyser: edge capture on up to three pins into the chip's SRAM,
optional trigger, time reconstruction, measurements and VCD export.

Runs the "la" firmware image (fw/la.s). Timestamps come from the lanes'
capture mode: every edge of a pin (after the two-flop synchroniser and the
optional glitch filter) is stamped with the cycle it was seen, so edges are
placed to one clock cycle (40 ns at the declared 25 MHz). All channels
share that delay, so intervals between edges are exact to +-1 cycle (the asynchronous
input is sampled on a clock edge).
"""

from .images import CLOCK_HZ, IMAGES
from .link import TempoError

NOPIN = 15
P_A, PP_A = 8, 128


class Capture:
    def __init__(self, names, initial, edges, start, overruns, clock_hz=CLOCK_HZ):
        self.names = names
        self.initial = initial  # level of each channel at the start
        self.edges = edges  # per channel: list of (cycle since start, new level)
        self.start = start
        self.overruns = overruns  # channels whose edge FIFO overran (edges lost)
        self.clock_hz = clock_hz

    def measure(self, ch):
        """Frequency, duty cycle and pulse widths of a periodic channel,
        from full periods between rising edges."""
        e = self.edges[ch]
        rises = [t for t, lv in e if lv == 1]
        if len(rises) < 2:
            raise TempoError("need at least two rising edges on channel %d" % ch)
        highs, lows = [], []
        for (t0, l0), (t1, _) in zip(e, e[1:]):
            (highs if l0 == 1 else lows).append(t1 - t0)
        period = (rises[-1] - rises[0]) / (len(rises) - 1)
        # mean high over mean high + mean low: a capture that starts and
        # ends on different levels holds one more interval of one kind
        if highs and lows:
            mh, ml = sum(highs) / len(highs), sum(lows) / len(lows)
            duty = mh / (mh + ml)
        else:
            duty = None
        return {
            "frequency_hz": self.clock_hz / period,
            "period_cycles": period,
            "duty": duty,
            "high_min": min(highs) if highs else None, "high_max": max(highs) if highs else None,
            "low_min": min(lows) if lows else None, "low_max": max(lows) if lows else None,
            "resolution_s": 1 / self.clock_hz,
        }

    def to_vcd(self, out):
        """Write a VCD (timescale 1 ns); PulseView and GTKWave read it."""
        ns = 1e9 / self.clock_hz
        ids = "!\"#$%"
        out.write("$timescale 1ns $end\n$scope module tempo $end\n")
        for k, n in enumerate(self.names):
            out.write("$var wire 1 %s %s $end\n" % (ids[k], n))
        out.write("$upscope $end\n$enddefinitions $end\n#0\n$dumpvars\n")
        for k in range(len(self.names)):
            out.write("%d%s\n" % (self.initial[k], ids[k]))
        out.write("$end\n")
        ev = sorted((t, k, lv) for k in range(len(self.names)) for t, lv in self.edges[k])
        last = None
        for t, k, lv in ev:
            if t != last:
                out.write("#%d\n" % int(round(t * ns)))
                last = t
            out.write("%d%s\n" % (lv, ids[k]))


class LogicAnalyser:
    def __init__(self, link, pins=(0, 1, 2), names=None, trigger=None, n=160, filter_cycles=0):
        """pins: up to three Tempo pin indices. trigger: None, or
        (pin, "fall" | "rise"), watched as lane 0's A pin; capture starts at
        that edge.
        n: records per channel (edges plus time markers), at most 160 with
        the default buffer (three regions of 160 words)."""
        if not 1 <= len(pins) <= 3:
            raise ValueError("one to three channels")
        self.link = link
        self.pins = list(pins)
        self.names = names or ["ch%d" % p for p in pins]
        self.n = n
        par = {"mode": 1 << 7 | (filter_cycles & 7) << 9, "trig": 0, "stride": max(n, 1)}
        for k in range(3):
            used = k < len(self.pins)
            p = self.pins[k] if used else NOPIN
            par["pins%d" % k] = NOPIN << 12 | NOPIN << 8 | p << 4 | NOPIN
            par["bit%d" % k] = (1 << p) if used else 0
            par["n%d" % k] = n if used else 0
        if trigger is not None:
            tp, edge = trigger
            par["pins0"] = (par["pins0"] & 0x0FFF) | tp << 12
            par["trigpat"] = (PP_A | P_A) << 8 | (PP_A if edge == "fall" else P_A)
            par["trig"] = 1
        link.load(IMAGES["la"], par)

    def arm(self):
        self.link.put([1])

    def stop(self):
        self.link.put([0])

    def capture(self, timeout_us=1000000):
        """Arm, wait until every channel's region is full (or timeout, then
        stop early), read the records back and rebuild the edges."""
        self.arm()
        try:
            reply = self.link.get(5, timeout_us)
        except TempoError:
            self.stop()
            reply = self.link.get(5, 20000)  # recorders stop within 4096 cycles
        counts = [c & 0x7FFF for c in reply[:3]]
        header, start = reply[3] | sum(((c >> 15) & 1) << (4 + k) for k, c in enumerate(reply[:3])), reply[4]
        buf = self.link.param_values["buf"]
        stride = self.link.param_values["stride"]
        regions = []
        for k in range(len(self.pins)):
            words = []
            for a in range(0, counts[k], 256):
                words += self.link.read_imem(buf + k * stride + a, min(256, counts[k] - a))
            regions.append(words)
        return self.decode(regions, header, start)

    def decode(self, regions, header, start):
        nch = len(self.pins)
        initial = [(header >> k) & 1 for k in range(nch)]
        overruns = [k for k in range(nch) if (header >> (4 + k)) & 1]
        edges = []
        for k in range(nch):
            out = []
            prev = 0  # cycles since start
            prev_low = start & 0x3FFF
            for w in regions[k]:
                code, low = w >> 14, w & 0x3FFF
                d = (low - prev_low) & 0x3FFF
                if d >= 0x2000:
                    d -= 0x4000  # a marker can be stamped a few cycles after a later-read edge
                t = prev + d
                prev, prev_low = t, low
                if code == 3:
                    continue  # time marker
                out.append((t, code))
            if out:
                initial[k] = 1 - out[0][1]
            edges.append(out)
        return Capture(self.names, initial, edges, start, overruns, self.link.clock_hz)
