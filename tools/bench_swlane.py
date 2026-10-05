#!/usr/bin/env python3
"""Software bit-banged UART TX against lane UART TX on Tempo itself.

Same chip model, same 25 MHz clock, same thread (T0); firmware in
fw/bench_swuart.s. Every number is measured on the lockstep-validated cycle
model (tools/tempo/sim.py); `--rtl` also runs the two-channel cases on the
RTL in lockstep with the model (cocotb, icarus). No pad timing model.

Run: python tools/bench_swlane.py [--rtl] [--output runs/bench_swlane_<date>.json]
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from tempo.clock import CLOCK_HZ  # noqa: E402
from tempo.harness import Harness  # noqa: E402
from tempo.models.uart import UartRx  # noqa: E402

CLOCK = float(CLOCK_HZ)
FW = "bench_swuart.s"
SOURCES = ["fw/bench_swuart.s", "fw/tempo.inc", "tools/bench_swlane.py", "tools/tempo/sim.py",
           "tools/tempo/lane.py", "tools/tempo/isa.py", "tools/tempo/asm.py",
           "tools/tempo/harness.py", "tools/tempo/board.py", "tools/tempo/models/uart.py"]
DATA = [0x55, 0xAA, 0x00, 0xFF, 0x0F, 0xF0, 0x55, 0x3C]
LONG = DATA + [0x81, 0x7E, 0x55, 0xAA, 0x01, 0x80, 0xC3, 0x5A]
NS = 1e9 / CLOCK
# Instructions on the per-byte path of each loop (static count from the source),
# used to separate UART work from background work in the polling variants.
LPOLL_BYTE_PATH = 8   # stat andi bne hget shli or tx bra
POLL_WORK_ITER = 5    # stat andi bne addi bra


def params(period):
    per = int(period)
    frac = round((period - per) * 256)
    if frac == 256:
        per, frac = per + 1, 0
    return per, frac


def frames(data):
    bits = []
    for b in data:
        bits += [0] + [(b >> i) & 1 for i in range(8)] + [1]
    return bits


def edges_of(trace, idx):
    out, prev = [], 1
    for cyc, val, oe in trace:
        lvl = (val >> idx) & 1 if (oe >> idx) & 1 else 1  # pull-up when released
        if lvl != prev:
            out.append(cyc)
            prev = lvl
    return out


def edge_errors(edges, data, period):
    """Edge times against the ideal grid t0 + n * period, t0 = first start edge.
    Returns None when the edge count does not match the expected stream."""
    bits = frames(data)
    want = [n for n in range(len(bits)) if bits[n] != (bits[n - 1] if n else 1)]
    if len(edges) < len(want):
        return None
    edges = edges[:len(want)]
    t0 = edges[0]
    stream = [e - (t0 + n * period) for e, n in zip(edges, want)]
    frame = []
    for e, n in zip(edges, want):
        f = n // 10
        s = edges[want.index(10 * f)]
        frame.append(e - (s + (n - 10 * f) * period))
    return {"stream_max_abs": max(abs(x) for x in stream),
            "stream_spread": max(stream) - min(stream),
            "frame_max_abs": max(abs(x) for x in frame),
            "end": t0 + len(bits) * period, "t0": t0}


def harness(entry, per, frac, load, perb=250, fracb=0):
    src = f'.include "{FW}"\n.entry 0, {entry}\n'
    threads = {0, 1, 2, 3} if load else {0}
    h = Harness(src, clock_hz=CLOCK, pullups={"uio0", "uio1"}, threads=threads)
    sym = h.prog.symbols
    for name, v in (("P_PER", per), ("P_FRAC", frac), ("P_PERB", perb), ("P_FRACB", fracb)):
        h.prog.words[sym[name]] = v
        h.chip.imem[sym[name]] = v
    h.chip.trace_retires = False
    return h


def run_single(entry, period, load=False, data=DATA):
    """One UART channel on uio0. Bytes come from H2T, kept topped up."""
    per, frac = params(period)
    baud = CLOCK / period
    h = harness(entry, per, frac, load)
    rx = h.attach(UartRx("uio0", baud, clock_hz=CLOCK))
    t = h.chip.threads[0]
    pushed = 0
    snaps = []
    limit = 400 + period * 10 * (len(data) + 2)
    for _ in range(int(limit)):
        while pushed < len(data) and len(h.chip.h2t) < 4:
            h.chip.h2t.append(data[pushed])
            pushed += 1
        snaps.append((t.retired, t.stalls, t.regs[7]))
        h.step()
    edges = edges_of(h.trace, 0)
    err = edge_errors(edges, data, period)
    r = {"entry": entry, "period_cycles": period, "baud": baud, "per": per, "frac": frac,
         "load": load, "bytes": len(data), "decoded": rx.bytes == list(data) and not rx.errors,
         "thread_late": bool(t.late), "lane_stat": h.chip.lanes[0].stat, "edges": edges}
    if err is None:
        r.update(continuous=False)
        return r
    # Steady-state window: whole frames, skipping the first two (the lane
    # queue fills ahead of the wire) and the last three (the feeder stops).
    nf = len(data) - 5
    a = int(err["t0"] + 2 * 10 * period)
    b = int(err["t0"] + (2 + nf) * 10 * period)
    slots = sum(1 for c in range(a, b) if c % 4 == 0)
    ret = snaps[b][0] - snaps[a][0]
    stl = snaps[b][1] - snaps[a][1]
    work = (snaps[b][2] - snaps[a][2]) & 0xFFFF if entry == "lpoll_main" else 0
    r.update(err)
    r.update(continuous=err["stream_max_abs"] < period / 2,
             window_frames=nf, slots=slots, retired=ret, stalled=stl,
             instr_per_byte=ret / nf,
             busy_fraction=ret / slots, occupied_fraction=(ret + stl) / slots,
             work_units=work)
    if entry == "lpoll_main":
        r["instr_per_byte"] = (ret - POLL_WORK_ITER * work) / nf
        r["uart_fraction"] = LPOLL_BYTE_PATH * nf / slots
        r["free_fraction"] = 1 - r["uart_fraction"]
    r["ok"] = r["decoded"] and r["continuous"] and not r["thread_late"]
    r["within_2pct"] = r["ok"] and err["stream_max_abs"] <= 0.02 * period
    return r


def run_dual(entry, pa, pb, load=False, nbytes=8):
    """Two UARTs from one thread: A on uio0 (period pa), B on uio1 (pb).
    Bytes are generated: seed + 1, seed + 2, ..."""
    per, frac = params(pa)
    perb, fracb = params(pb)
    h = harness(entry, per, frac, load, perb, fracb)
    seeds = h.prog.words[h.prog.symbols["P_SEEDA"]], h.prog.words[h.prog.symbols["P_SEEDB"]]
    rxa = h.attach(UartRx("uio0", CLOCK / pa, clock_hz=CLOCK))
    rxb = h.attach(UartRx("uio1", CLOCK / pb, clock_hz=CLOCK))
    t = h.chip.threads[0]
    snaps = []
    limit = 600 + max(pa, pb) * 10 * (nbytes + 1)
    for _ in range(int(limit)):
        snaps.append((t.retired, t.stalls, t.regs[7]))
        h.step()
    out = {"entry": entry, "load": load, "thread_late": bool(t.late), "channels": []}
    for idx, p, seed, rx in ((0, pa, seeds[0], rxa), (1, pb, seeds[1], rxb)):
        data = [(seed + 1 + k) & 0xFF for k in range(nbytes)]
        edges = edges_of(h.trace, idx)
        err = edge_errors(edges, data, p) or {}
        ch = {"period_cycles": p, "baud": CLOCK / p,
              "decoded": rx.bytes[:nbytes] == data and not rx.errors,
              "edges": edges}
        ch.update({k: v for k, v in err.items() if k in ("stream_max_abs", "frame_max_abs", "stream_spread")})
        ch["within_2pct"] = bool(err) and ch["decoded"] and err["stream_max_abs"] <= 0.02 * p
        out["channels"].append(ch)
    if entry == "ldual_main":
        a = 600
        b = int(600 + min(pa, pb) * 10 * nbytes)
        slots = sum(1 for c in range(a, b) if c % 4 == 0)
        out["work_units"] = (snaps[b][2] - snaps[a][2]) & 0xFFFF
        out["work_slot_fraction"] = POLL_WORK_ITER * out["work_units"] / slots
    return out


def strip(r):
    return {k: v for k, v in r.items() if k not in ("edges", "lane_stat")}


def sweep(entry, periods):
    rows = [run_single(entry, p) for p in periods]
    ok = [r for r in rows if r["ok"]]
    good = [r for r in rows if r["within_2pct"]]
    worst = max(r.get("stream_max_abs", 0) for r in ok) if ok else None
    return {"entry": entry, "periods": periods,
            "max_sustainable": min(ok, key=lambda r: r["period_cycles"]) if ok else None,
            "max_sustainable_fractional_period": min((r for r in ok if r["period_cycles"] % 1), default=None,
                                                     key=lambda r: r["period_cycles"]),
            "max_within_2pct": min(good, key=lambda r: r["period_cycles"]) if good else None,
            "max_abs_error_cycles": worst,
            # Edge error does not grow with the period (it is slot quantization
            # plus fraction rounding), so 2 % at an arbitrary rate needs a
            # period of at least worst / 0.02 cycles.
            "any_rate_2pct_min_period": None if worst is None else worst / 0.02,
            "any_rate_2pct_max_baud": None if not worst else CLOCK * 0.02 / worst,
            "rows": [strip(r) for r in rows]}


def manifest():
    return {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in SOURCES}


def git_head():
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def summary_row(r):
    return {"baud": round(r["baud"]), "period_cycles": r["period_cycles"],
            "instr_per_byte": r.get("instr_per_byte"), "busy_fraction": r.get("busy_fraction"),
            "occupied_fraction": r.get("occupied_fraction"), "free_fraction": r.get("free_fraction"),
            "max_edge_err_cycles": r.get("stream_max_abs"),
            "max_frame_relative_err_cycles": r.get("frame_max_abs"),
            "max_edge_err_ns": None if r.get("stream_max_abs") is None else r["stream_max_abs"] * NS,
            "err_pct_of_bit": None if r.get("stream_max_abs") is None else 100 * r["stream_max_abs"] / r["period_cycles"],
            "ok": r["ok"] if "ok" in r else False, "within_2pct": r.get("within_2pct", False),
            "thread_late": r["thread_late"]}


def measure():
    rates = {"115200": CLOCK / 115200, "1000000": CLOCK / 1e6}
    rep = {"clock_hz": CLOCK, "fw": "fw/" + FW, "data_cases": LONG, "data_sweeps": DATA,
           "cases": {}, "load_independent": {}}
    for name, entry in (("software", "sw_main"), ("lane", "lane_main"), ("lane_poll", "lpoll_main")):
        for rate, p in rates.items():
            quiet = run_single(entry, p, load=False, data=LONG)
            busy = run_single(entry, p, load=True, data=LONG)
            rep["cases"][f"{name}@{rate}"] = {"no_load": summary_row(quiet), "load": summary_row(busy)}
            rep["load_independent"][f"{name}@{rate}"] = quiet["edges"] == busy["edges"]
    # Rate sweeps (8 bytes each). Integer periods find the fastest continuous
    # stream; 1/8-cycle steps cover every slot phase and fraction pattern
    # that sets the worst edge error.
    eighths = lambda lo, hi: [lo + k / 8 for k in range(int((hi - lo) * 8) + 1)]  # noqa: E731
    rep["sweep"] = {
        "software": sweep("sw_main", list(range(16, 33)) + [p for p in eighths(28, 40) if p % 1]),
        "lane": sweep("lane_main", list(range(1, 13)) + [p for p in eighths(16, 20) if p % 1]),
    }
    case_err = {"software": [], "lane": []}
    for k, c in rep["cases"].items():
        name = "software" if k.startswith("software") else "lane"
        case_err[name] += [c["no_load"]["max_edge_err_cycles"], c["load"]["max_edge_err_cycles"]]
    for name, s in rep["sweep"].items():
        worst = max([s["max_abs_error_cycles"]] + case_err[name])
        s["max_abs_error_cycles_incl_cases"] = worst
        s["any_rate_2pct_min_period"] = worst / 0.02
        s["any_rate_2pct_max_baud"] = CLOCK * 0.02 / worst
    for s in rep["sweep"].values():
        for key in ("max_sustainable", "max_sustainable_fractional_period", "max_within_2pct"):
            if s[key] is not None:
                s[key] = summary_row(s[key])
    # Shipped console firmware (fw/uart.s T0), for its per-byte instruction cost.
    rep["shipped_uart_s"] = uart_s_cost()
    # A second UART on the same thread.
    rep["dual"] = {}
    for pa, pb in ((217, 250), (40, 48)):
        for entry in ("swdual_main", "ldual_main"):
            for load in (False, True):
                d = run_dual(entry, pa, pb, load)
                key = f"{entry}@{pa}/{pb}{'+load' if load else ''}"
                rep["dual"][key] = d
    for k, d in rep["dual"].items():
        if k.endswith("+load"):
            base = rep["dual"][k[:-5]]
            d["load_independent"] = all(a["edges"] == b["edges"] for a, b in zip(base["channels"], d["channels"]))
    for d in rep["dual"].values():
        for ch in d["channels"]:
            ch.pop("edges")
    return rep


def uart_s_cost(nchars=8, period=CLOCK / 115200):
    """T0 instructions per character of the shipped console bridge (fw/uart.s)."""
    h = Harness('.include "uart.s"\n', clock_hz=CLOCK, pullups={"uio0", "uio1"}, threads={0})
    per, frac = params(period)
    for name, v in (("P_LEN", per), ("P_FRAC", frac)):
        h.chip.imem[h.prog.symbols[name]] = v
    h.chip.trace_retires = False
    data = DATA[:nchars]
    words = [nchars] + [data[i] | data[i + 1] << 8 for i in range(0, nchars, 2)]
    rx = h.attach(UartRx("uio0", CLOCK / period, clock_hz=CLOCK))
    t = h.chip.threads[0]
    h.run(400)
    r0 = t.retired
    pushed = 0
    for _ in range(int(period * 10 * (nchars + 2))):
        while pushed < len(words) and len(h.chip.h2t) < 4:
            h.chip.h2t.append(words[pushed])
            pushed += 1
        h.step()
    # The thread ends blocked in hget for the next header, so every retire
    # after setup belongs to the header, the words and the characters.
    assert rx.bytes == data and not rx.errors, (rx.bytes, rx.errors)
    return {"chars": nchars, "instr_per_char": (t.retired - r0) / nchars,
            "note": "includes header and word unpacking, parity/format checks from the parameter block"}


def run_rtl(log):
    env = dict(os.environ, COCOTB_TEST_MODULES="bench_swlane",
               PYTHONPATH=os.pathsep.join([str(ROOT / "tools"), str(ROOT / "test"),
                                           os.environ.get("PYTHONPATH", "")]))
    with open(log, "w") as f:
        r = subprocess.run(["make", "-B"], cwd=ROOT / "test", env=env, stdout=f, stderr=subprocess.STDOUT)
    text = Path(log).read_text()
    return {"returncode": r.returncode, "log": str(Path(log).relative_to(ROOT)),
            "passed": r.returncode == 0 and "FAIL=0" in text}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    p.add_argument("--rtl", action="store_true", help="also run the dual cases on RTL (cocotb lockstep)")
    a = p.parse_args()
    date = datetime.date.today().strftime("%Y%m%d")
    out = a.output or ROOT / "runs" / f"bench_swlane_{date}.json"
    rep = {"evidence": "cycle model (tools/tempo/sim.py), lockstep-validated against RTL; no pad model",
           "head": git_head(), "source_sha256": manifest()}
    rep.update(measure())
    if a.rtl:
        rep["rtl"] = run_rtl(ROOT / "runs" / f"bench_swlane_rtl_{date}.log")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=1) + "\n")
    print_table(rep)
    print(f"wrote {out}")


def print_table(rep):
    def f(x, fmt):
        return "-" if x is None else format(x, fmt)
    print(f"{'case':28} {'instr/B':>8} {'busy':>6} {'occ':>6} {'free':>6} {'err cyc':>8} {'err ns':>7} {'%bit':>6} {'2%':>3} load-indep")
    for k, c in rep["cases"].items():
        r = c["no_load"]
        print(f"{k:28} {f(r['instr_per_byte'], '8.1f')} {f(r['busy_fraction'], '6.3f')} "
              f"{f(r['occupied_fraction'], '6.3f')} {f(r['free_fraction'], '6.3f')} "
              f"{f(r['max_edge_err_cycles'], '8.2f')} {f(r['max_edge_err_ns'], '7.1f')} "
              f"{f(r['err_pct_of_bit'], '6.2f')} {'y' if r['within_2pct'] else 'n':>3} "
              f"{rep['load_independent'][k]}")
    for k, s in rep["sweep"].items():
        ms, m2 = s["max_sustainable"], s["max_within_2pct"]
        print(f"{k:10} max sustainable {ms and ms['baud']} baud (T={ms and ms['period_cycles']}), "
              f"(fractional T: {s['max_sustainable_fractional_period'] and s['max_sustainable_fractional_period']['period_cycles']}), "
              f"max within 2% {m2 and m2['baud']} (T={m2 and m2['period_cycles']}), "
              f"worst err {s['max_abs_error_cycles_incl_cases']:.3f} cyc -> any rate within 2% up to "
              f"{s['any_rate_2pct_max_baud'] and round(s['any_rate_2pct_max_baud'])} baud")
    print("uart.s T0 instr/char:", rep["shipped_uart_s"]["instr_per_char"])
    for k, d in rep["dual"].items():
        chs = ", ".join(f"T={c['period_cycles']} dec={c['decoded']} err={c.get('stream_max_abs')} 2%={c['within_2pct']}"
                        for c in d["channels"])
        print(f"{k:28} late={d['thread_late']} {chs} work={d.get('work_slot_fraction')} "
              f"load-indep={d.get('load_independent')}")
    if "rtl" in rep:
        print("rtl:", rep["rtl"])


if os.environ.get("COCOTB_TEST_MODULES") == "bench_swlane":
    import cocotb
    from lockstep import Lockstep

    async def _rtl_dual(dut, entry, pa=217, pb=250, nbytes=8):
        src = f'.include "{FW}"\n.entry 0, {entry}\n'
        ls = Lockstep(dut, src, pullups={"uio0", "uio1"}, boot="backdoor")
        sym = ls.h.prog.symbols
        for name, v in (("P_PER", pa), ("P_FRAC", 0), ("P_PERB", pb), ("P_FRACB", 0)):
            ls.h.prog.words[sym[name]] = v
            ls.chip.imem[sym[name]] = v
        seeds = ls.h.prog.words[sym["P_SEEDA"]], ls.h.prog.words[sym["P_SEEDB"]]
        rxa = ls.attach(UartRx("uio0", CLOCK / pa, clock_hz=CLOCK))
        rxb = ls.attach(UartRx("uio1", CLOCK / pb, clock_hz=CLOCK))
        rec = ls.attach(_EdgeRecorder())
        await ls.start()
        await ls.run(600 + max(pa, pb) * 10 * (nbytes + 1))
        model = run_dual(entry, pa, pb, load=True, nbytes=nbytes)
        for idx, (rx, seed, p) in enumerate(((rxa, seeds[0], pa), (rxb, seeds[1], pb))):
            data = [(seed + 1 + k) & 0xFF for k in range(nbytes)]
            assert rx.bytes[:nbytes] == data and not rx.errors, (rx.bytes, rx.errors)
            err = edge_errors(rec.edges[idx], data, p)
            assert err is not None
            want = model["channels"][idx]
            assert err["stream_max_abs"] == want["stream_max_abs"], (err, want)
            assert err["frame_max_abs"] == want["frame_max_abs"], (err, want)
            dut._log.info(f"{entry} ch{idx}: RTL edge error {err['stream_max_abs']} cycles "
                          f"(model {want['stream_max_abs']})")
        dut._log.info(f"{entry}: {ls.cycles} cycles, {ls.commits} commits matched")

    class _EdgeRecorder:
        """Edge cycles on uio0/uio1 as resolved on the board (RTL outputs)."""
        def __init__(self):
            self.edges = ([], [])
            self.prev = [1, 1]

        def step(self, cycle, nets):
            for i in (0, 1):
                lvl = nets.get(f"uio{i}", 1)
                if lvl != self.prev[i]:
                    self.edges[i].append(cycle)
                    self.prev[i] = lvl
            return {}

    @cocotb.test()
    async def test_rtl_swdual_with_load(dut):
        await _rtl_dual(dut, "swdual_main")

    @cocotb.test()
    async def test_rtl_ldual_with_load(dut):
        await _rtl_dual(dut, "ldual_main")


if __name__ == "__main__":
    main()
