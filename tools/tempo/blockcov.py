"""Coverage of the blocking instructions (wev, hget, rx, hput) on the model.

Wrap every `Chip.step` with `before()` / `after()`. Per instruction it counts
executions that stalled (blocked cycles), retirements that followed a stall
(woke), retirements that found the source ready on the first try
(immediate), wake latency (cycles from the source becoming visible to the
retirement), and how often two or more threads were blocked on the same
source at once. Read-only: it never changes model state.
"""

import collections

from . import isa
from .sim import FIFO_DEPTH

BLOCKING = ("wev", "hget", "rx", "hput")


class BlockCov:
    def __init__(self, chip):
        self.chip = chip
        self.blocked = collections.Counter()
        self.woke = collections.Counter()
        self.immediate = collections.Counter()
        self.latency = collections.defaultdict(collections.Counter)
        self.multi = collections.Counter()  # episodes with >= 2 threads blocked on one source
        self.waiting = {}  # tid -> (pc, key)
        self.since = {}  # key -> cycle the source became visible
        self._multi_keys = set()
        self._pre = None

    def key(self, name, insn):
        if name == "wev":
            return ("wev", insn.rs)
        if name == "rx":
            return ("rx", insn.lane)
        return (name,)

    def avail(self, key):
        c = self.chip
        if key[0] == "wev":
            return (c.ev >> key[1]) & 1
        if key[0] == "rx":
            return key[1] < len(c.lanes) and bool(c.lanes[key[1]].rxfifo)
        if key[0] == "hget":
            return bool(c.h2t)
        return len(c.t2h) < FIFO_DEPTH

    def keys(self):
        return [("wev", e) for e in range(8)] + [("rx", k) for k in range(len(self.chip.lanes))] + \
            [("hget",), ("hput",)]

    def before(self):
        c = self.chip
        if not self.since:
            self.since = {k: c.now for k in self.keys() if self.avail(k)}
        t = c.threads[c.now % isa.NTHREADS]
        i = isa.decode(t.ir)
        plain = not (t.ldi_phase or t.sti_phase)
        self._pre = (c.now, t, t.pc, i, t.stalls, t.retired, plain)

    def after(self):
        now, t, pc, i, stalls, retired, plain = self._pre
        name = i.name
        if plain and name in BLOCKING and not (name == "rx" and i.lane >= isa.LANES):
            k = self.key(name, i)
            if t.stalls > stalls:
                self.blocked[name] += 1
                self.waiting[t.tid] = (pc, k)
            elif t.retired > retired:
                if self.waiting.get(t.tid) == (pc, k):
                    self.woke[name] += 1
                    self.latency[name][now - self.since.get(k, now)] += 1
                else:
                    self.immediate[name] += 1
                self.waiting.pop(t.tid, None)
        for tid, th in enumerate(self.chip.threads):
            if not th.run:
                self.waiting.pop(tid, None)
        groups = collections.Counter(k for _, k in self.waiting.values())
        now_multi = {k for k, n in groups.items() if n >= 2}
        for k in now_multi - self._multi_keys:
            self.multi[k[0]] += 1
        self._multi_keys = now_multi
        nxt = self.chip.now
        for k in self.keys():
            if self.avail(k):
                self.since.setdefault(k, nxt)
            else:
                self.since.pop(k, None)

    def merge(self, other):
        for a in ("blocked", "woke", "immediate", "multi"):
            getattr(self, a).update(getattr(other, a))
        for n, h in other.latency.items():
            self.latency[n].update(h)

    def report(self):
        out = []
        for n in BLOCKING:
            lat = self.latency[n]
            lo = min(lat) if lat else "-"
            out.append(f"{n}: immediate={self.immediate[n]} blocked_cycles={self.blocked[n]} woke={self.woke[n]} "
                       f"multi_blocked={self.multi[n]} wake_latency_min={lo} latency0={lat[0]} "
                       f"latency_hist={dict(sorted(lat.items())[:8])}")
        return out
