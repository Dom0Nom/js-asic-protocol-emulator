#!/usr/bin/env python3
"""Result tables in README.md and docs/, generated from results/.

results/ holds the signoff metrics of the hardened design and one summary
per test log. Every summary starts with the header line its runner printed:
commit, number of modified tracked files, clock.

    tools/results.py add <name> <log>   store the summary of a log as results/<name>.log
    tools/results.py                    rewrite the tables
    tools/results.py --check            fail if a table is stale
"""

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
KEEP = re.compile(r"^commit |TESTS=\d+|\d+ passed|\d+ failed| (passed|failed)$|commits matched|DONE \(|"
                  r"sha256|^[0-9a-f]{64} |Precheck passed|precheck rc=|sdf cells|^wall=|rc=\d+")

# name, label, what a pass looks like
LOGS = [
    ("model", "Firmware, host library and wire models on the cycle model (pytest)"),
    ("rtl", "The same firmware on the RTL, in lockstep with the model (cocotb)"),
    ("random", "Constrained-random lockstep"),
    ("formal_core", "Core properties, k-induction"),
    ("formal_core_cover", "Core cover statements"),
    ("formal_lane_anchor", "Lane start-time anchor, BMC depth 40 and covers"),
    ("formal_lane_miter", "Lane timing-independence miter, BMC (bounded)"),
    ("gate", "Routed netlist, zero delay"),
    ("gate_sdf_slow", "Routed netlist, SDF slow corner"),
    ("gate_sdf_fast", "Routed netlist, SDF fast corner"),
]


def summarise(log):
    lines = Path(log).read_text(errors="replace").splitlines()
    out = [ln.strip() for ln in lines if KEEP.search(ln)]
    return "\n".join(ln[-200:] for ln in out) + "\n"


def outcome(text):
    """One-line result of a summary."""
    passed = failed = 0
    for m in re.finditer(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+)", text):
        passed += int(m.group(2))
        failed += int(m.group(3))
    for m in re.finditer(r"(\d+) passed", text):
        passed += int(m.group(1))
    for m in re.finditer(r"(\d+) failed", text):
        failed += int(m.group(1))
    parts = []
    if passed or failed:
        parts.append("%d passed, %d failed" % (passed, failed))
    m = re.findall(r"(\d+) commits matched", text)
    if m:
        parts.append("{:,} instruction commits matched".format(sum(map(int, m))))
    sby = re.findall(r"\[(\w+)\] DONE \((\w+)", text)
    parts += ["%s %s" % (task.split("_")[-1], res) for task, res in sby]
    return "; ".join(parts) or "no result"


def header(text):
    m = re.match(r"commit (\w+) dirty_tracked=(\d+)(?: clock_hz=(\d+))?", text)
    return m.groups() if m else ("?", "?", None)


def metrics():
    with open(RES / "metrics.csv") as f:
        return {r[0]: r[1] for r in csv.reader(f) if len(r) == 2}


def tables():
    m = metrics()
    hard = (RES / "harden.txt").read_text().split()
    commit, period = hard[0], float(hard[1])
    mhz = 1000 / period
    slow, fast = "nom_slow_1p08V_125C", "nom_fast_1p32V_m40C"
    clock = "%g MHz" % round(mhz, 2)
    pre = (RES / "precheck.txt").read_text()
    rows = [
        ("Clock", "%s (%.3f ns)" % (clock, period)),
        ("Setup slack, slow corner (1.08 V, 125 C)", "%+.2f ns" % float(m["timing__setup__ws__corner:" + slow])),
        ("Hold slack, fast corner (1.32 V, -40 C)", "%+.3f ns" % float(m["timing__hold__ws__corner:" + fast])),
        ("Standard cells", "{:,}".format(int(m["design__instance__count__stdcell"]))),
        ("Utilisation (cells and SRAM)", "%.0f%% of 6x4 tiles" % (100 * float(m["design__instance__utilization"]))),
        ("Routing DRC, KLayout DRC, LVS, antenna", "%s, %s, %s, %s" % (
            m["route__drc_errors"], m["klayout__drc_error__count"], m["design__lvs_error__count"],
            m["route__antenna_violation__count"])),
        ("Max slew, max capacitance, max fanout violations", "%s, %s, %s" % (
            m["design__max_slew_violation__count"], m["design__max_cap_violation__count"],
            m["design__max_fanout_violation__count"])),
        ("Tiny Tapeout precheck", "passed" if "Precheck passed" in pre else "FAILED"),
    ]
    detail = []
    for name, label in LOGS:
        p = RES / (name + ".log")
        if not p.exists():
            continue
        text = p.read_text()
        c, dirty, hz = header(text)
        rows.append((label, outcome(text)))
        detail.append((label, outcome(text), c, dirty, "" if hz is None else "%g MHz" % (int(hz) / 1e6)))
    rows.append(("Hardened commit", "`%s`" % commit))
    short = "| | |\n|---|---|\n" + "\n".join("| %s | %s |" % r for r in rows) + "\n"
    full = ("| Check | Result |\n|---|---|\n" + "\n".join("| %s | %s |" % r for r in rows[:8]) +
            "\n\n| Run | Result | Commit | Modified files | Clock |\n|---|---|---|---|---|\n" +
            "\n".join("| %s | %s | `%s` | %s | %s |" % d for d in detail) + "\n")
    return {"README.md": {"results": short}, "docs/verification.md": {"results": full},
            "docs/info.md": {"clock": clock}}


def apply(check):
    stale = []
    for name, blocks in tables().items():
        path = ROOT / name
        text = new = path.read_text()
        for key, body in blocks.items():
            a, b = "<!-- %s:begin -->" % key, "<!-- %s:end -->" % key
            sep = "" if key == "clock" else "\n"
            new = re.sub(re.escape(a) + ".*?" + re.escape(b), lambda _: a + sep + body + b, new, flags=re.S)
        if new != text:
            stale.append(name)
            if not check:
                path.write_text(new)
    if check and stale:
        sys.exit("stale result tables: " + ", ".join(stale))


if __name__ == "__main__":
    if sys.argv[1:2] == ["add"]:
        (RES / (sys.argv[2] + ".log")).write_text(summarise(sys.argv[3]))
    else:
        apply("--check" in sys.argv)
