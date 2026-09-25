"""Constrained-random Tempo programs for model-vs-RTL lockstep.

Each thread gets its own random block. Control flow only goes forward, so
every program terminates or ends in a bounded wait, and lane settings stay
small so slots are short. Stalls are allowed: a stalled thread is still
compared cycle by cycle. The model is the oracle; the generator only has to
produce legal, interesting instruction mixes.
"""

import random

from . import isa

LANE_REG_RANGES = {
    "cfg": (0, 0x3FFF), "pins": None, "len0": (1, 12), "len1": (1, 12),
    "sym0": None, "sym1": None, "frac": (0, 255), "cr": (0, 10), "cf": (0, 12),
    "samp": (0, 10), "pat": (0, 0xFFFF), "pate": (0, 0xFFFF), "crc": (0, 0xFFFF),
    "poly": (0, 0xFFFF), "mode": None,
}


def rand_pins(rng):
    def pin():
        return rng.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15])
    return pin() | pin() << 4 | pin() << 8 | pin() << 12


def lane_value(rng, reg):
    if reg == "pins":
        return rand_pins(rng)
    if reg in ("sym0", "sym1"):
        return rng.getrandbits(3) << 13 | rng.randint(0, 12)
    if reg == "mode":
        # small prescaler, no capture most of the time
        return rng.randint(0, 1) | rng.getrandbits(1) << 3 | rng.getrandbits(3) << 4 | \
            (rng.random() < 0.1) << 7 | rng.getrandbits(1) << 8 | rng.randint(0, 2) << 9
    lo, hi = LANE_REG_RANGES[reg]
    return rng.randint(lo, hi)


def thread_block(rng, tid, n):
    """Assembly lines for one thread: setup, n random instructions, halt."""
    L = []
    lab = lambda k: f"t{tid}_{k}"  # noqa: E731
    for r in range(8):
        L.append(f"lc r{r}, {rng.getrandbits(16)}")
    for k in range(n):
        L.append(f"{lab(k)}:")
        L.extend(random_insn(rng, tid, k, n, lab))
    L.append(f"{lab(n)}: halt")
    return L


def random_insn(rng, tid, k, n, lab):
    r = lambda: f"r{rng.randint(0, 7)}"  # noqa: E731
    lane = f"l{rng.randint(0, 3)}"
    fwd = lab(min(n, k + rng.randint(1, 4)))
    c = rng.random()
    if c < 0.22:
        return [f"{rng.choice(isa.ALU3_FN)} {r()}, {r()}, {r()}"]
    if c < 0.32:
        op = rng.choice(isa.ALU2_FN + list(isa.ALU2_SHIFT.values()))
        if op in isa.ALU2_SHIFT.values():
            return [f"{op} {r()}, {r()}, {rng.randint(0, 15)}"]
        return [f"{op} {r()}, {r()}"]
    if c < 0.42:
        op = rng.choice(["li", "addi", "cmpi", "andi"])
        return [f"{op} {r()}, {rng.randint(-256, 255)}"]
    if c < 0.47:
        op = rng.choice(["lih", "ori", "xori"])
        return [f"{op} {r()}, {rng.randint(0, 255 if op == 'lih' else 127)}"]
    if c < 0.53:
        return [f"{rng.choice(['ld', 'st'])} {r()}, [{r()} + {rng.randint(0, 31)}]"]
    if c < 0.58:
        return [f"b {rng.choice(isa.COND)}, {fwd}"]
    if c < 0.60:
        return [f"j {fwd}"]
    if c < 0.62:
        return [f"ldi {r()}, [{r()} + {rng.randint(0, 63)}]"]
    if c < 0.72:
        op = rng.choice(["nop", "now", "ior", "iow", "ioe", "sev", "tid", "hcnt", "tstat", "hput"])
        if op == "nop":
            return ["nop"]
        if op == "sev":
            return [f"sev {rng.randint(0, 7)}"]
        if op in ("iow", "ioe"):
            return [f"{op} {r()}, {r()}"]
        return [f"{op} {r()}"]
    if c < 0.76:
        # bounded waits: deadline a little ahead of now
        d = f"r{rng.randint(0, 7)}"
        w = ["now " + d, f"addi {d}, {rng.randint(0, 40)}"]
        if rng.random() < 0.5:
            return w + [f"waitt {d}"]
        return w + [f"wany {d}, {r()}"]
    if c < 0.84:
        reg = rng.choice(list(LANE_REG_RANGES))
        val = lane_value(rng, reg)
        return [f"lc r7, {val}", f"lw {lane}, {reg}, r7"]
    if c < 0.90:
        return [f"tx {lane}, {r()}, {rng.randint(1, 16)}, {rng.choice(['', 'x', 'c', 'xc'])}".rstrip(", ")]
    if c < 0.92:
        return [f"txcrc {lane}, {rng.randint(1, 16)}"]
    if c < 0.94:
        return [f"rxn {lane}, {rng.randint(1, 16)}"]
    if c < 0.96:
        return [f"emit {lane}, {r()}"]
    if c < 0.975:
        return [f"{rng.choice(['start', 'sync'])} {lane}, {r()}"]
    if c < 0.985:
        return [f"stat {r()}, {lane}"]
    if c < 0.99:
        return [f"lr {r()}, {lane}, {rng.choice(isa.LANE_REG)}"]
    if c < 0.995:
        return [f"stop {lane}"]
    return [f"wait {lane}, {rng.choice(['idle', 'rx'])}"]


def program(seed, n=40, threads=4):
    rng = random.Random(seed)
    lines = []
    for t in range(threads):
        lines.append(f".entry {t}, t{t}_start")
        lines.append(f"t{t}_start:")
        lines.extend(thread_block(rng, t, n))
    return "\n".join(lines) + "\n"
