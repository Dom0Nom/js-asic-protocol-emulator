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
            (rng.random() < 0.1) << 7 | rng.getrandbits(1) << 8 | rng.randint(0, 2) << 9 | \
            (rng.randint(1, 3) if rng.random() < 0.4 else 0) << 12  # external-clock receive
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
    L.append(f"t{tid}_sub: addi r{rng.randint(0, 6)}, 1")
    L.append("ret")
    return L


def random_insn(rng, tid, k, n, lab):
    r = lambda: f"r{rng.randint(0, 7)}"  # noqa: E731
    # lanes 0-2 exist; lane 3 was cut, so its ops are illegal (rarely drawn)
    lane = f"l{rng.randint(0, 2) if rng.random() < 0.98 else 3}"
    fwd = lab(min(n, k + rng.randint(1, 4)))
    c = rng.random()
    if c < 0.22:
        return [f"{rng.choice(isa.ALU3_FN)} {r()}, {r()}, {r()}"]
    if c < 0.32:
        op = rng.choice([f for f in isa.ALU2_FN if f != "sti"] + list(isa.ALU2_SHIFT.values()))
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
    if c < 0.595:
        return [f"j {fwd}"]
    if c < 0.60:
        return [f"call t{tid}_sub"]
    if c < 0.605:
        return [f"li r6, {rng.randint(1, 3)}", f"djnz r6, {fwd}"]
    if c < 0.615:
        # guarded blocking ops: wait for the source with a deadline, then
        # take it only if it is ready (rx, hget, wev would block forever)
        k = rng.randint(0, 2)
        src, take = rng.choice([
            (1 << k, f"rx {r()}, l{k}"),
            (1 << 8, f"hget {r()}"),
            (1 << (10 + (e := rng.randint(0, 5))), f"wev {e}"),
        ])
        return ["now r6", f"addi r6, {rng.randint(4, 60)}", f"lc r5, {src}", "wany r6, r5",
                "cmpi r6, 0", f"beq {fwd}", take]
    if c < 0.625:
        return [f"ldi {r()}, [{r()} + {rng.randint(0, 63)}]"]
    if c < 0.635:
        # stores mostly into a data area above the code, rarely anywhere
        # (self-modifying code is still compared cycle by cycle)
        if rng.random() < 0.9:
            return [f"lc r7, {0x380 + rng.randint(0, 127)}", f"sti {r()}, r7"]
        return [f"sti {r()}, {r()}"]
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
    if c < 0.80:
        # external-clock receive on a noisy input pin (ui0-4 = index 8-12)
        pins = rng.choice([15, 0, 1]) | rng.randint(8, 12) << 4 | rng.randint(8, 12) << 8 | 15 << 12
        mode = rng.randint(1, 3) << 12 | rng.randint(0, 2) << 9
        return [f"lc r7, {pins}", f"lw {lane}, pins, r7", f"lc r7, {mode}", f"lw {lane}, mode, r7",
                f"rxn {lane}, {rng.randint(1, 16)}"]
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


def blocking_program(seed, threads=4, body=12):
    """Program biased toward the blocking instructions (wev, hget, rx, hput).

    One or two producer threads loop forever: a random delay, then sev on a
    small per-seed event set, rxn on a small per-seed lane set, or hput.
    The other threads loop over unguarded wev/hget/rx with random delays
    (0 to 24 cycles), so each op sometimes finds its source already pending
    and sometimes blocks, and small event and lane sets make several
    threads block on the same source. hget is fed by the host driver.
    Producers never block indefinitely, so consumers keep making progress.
    """
    rng = random.Random(seed ^ 0xB10C)
    events = rng.sample(range(8), rng.randint(1, 3))
    lanes = rng.sample(range(3), rng.randint(1, 2))
    producers = set(rng.sample(range(threads), rng.randint(1, 2)))
    lines = []

    def delay(L, tag):
        k = rng.choice([0, 0, 1, 2, 3, 5, 8, 13, 24])
        if k == 0:
            return
        if rng.random() < 0.5:
            L += ["now r6", f"addi r6, {k}", "waitt r6"]
        else:
            L += ["nop"] * min(k, 4)

    for t in range(threads):
        lines += [f".entry {t}, b{t}_start", f"b{t}_start:"]
        L = []
        if t in producers:
            for ln in lanes:
                pins = 15 | rng.randint(8, 12) << 4 | 15 << 8 | 15 << 12  # I on a noisy ui pin
                L += [f"lc r7, {pins}", f"lw l{ln}, pins, r7",
                      f"li r7, {rng.randint(1, 3)}", f"lw l{ln}, len0, r7", f"lw l{ln}, len1, r7"]
        L.append(f"b{t}_loop:")
        for k in range(body):
            delay(L, k)
            c = rng.random()
            if t in producers:
                if c < 0.55:
                    L.append(f"sev {rng.choice(events)}")
                elif c < 0.9:
                    L.append(f"rxn l{rng.choice(lanes)}, {rng.randint(1, 8)}")
                else:
                    L += [f"li r1, {rng.randint(0, 255)}", "hput r1"]
            else:
                if c < 0.4:
                    L.append(f"wev {rng.choice(events)}")
                elif c < 0.65:
                    L.append(f"hget r{rng.randint(0, 5)}")
                elif c < 0.9:
                    L.append(f"rx r{rng.randint(0, 5)}, l{rng.choice(lanes)}")
                elif c < 0.95:
                    L += [f"lc r5, {sum(1 << (10 + e) for e in events if e < 6) | 1 << 8}",
                          "now r6", f"addi r6, {rng.randint(1, 30)}", "wany r6, r5"]
                else:
                    L.append(f"hput r{rng.randint(0, 5)}")
        L.append(f"j b{t}_loop")
        lines += L
    return "\n".join(lines) + "\n"
