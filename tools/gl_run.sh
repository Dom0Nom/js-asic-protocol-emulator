#!/usr/bin/env bash
# Gate-level cocotb run against a netlist, in a scratch copy of test/ so it can
# run beside the RTL suite.
# usage: tools/gl_run.sh <netlist.v> <tag> ["<test names, space separated>"] [modules]
# Without test names it runs the gate-level subset set in test/Makefile.
# SDF=<file> back-annotates timing.
# PDK_ROOT must hold ihp-sg13cmos5l/libs.ref (default: a link to ./pdk).
set -e; unset CDPATH
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
NL=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); TAG=$2; TESTS=${3:-}; MODS=${4:-test,test_usecases,test_ps2,test_can}
GL=$ROOT/runs/gl; D=$GL/work_$TAG
if [ -z "${PDK_ROOT:-}" ]; then
  mkdir -p "$GL/pdkroot"; [ -e "$GL/pdkroot/ihp-sg13cmos5l" ] || ln -s "$ROOT/pdk" "$GL/pdkroot/ihp-sg13cmos5l" 2>/dev/null || true
  PDK_ROOT=$GL/pdkroot
fi
export PDK_ROOT
rm -rf "$D"; mkdir -p "$D"
rsync -a --exclude sim_build --exclude results.xml --exclude '*.fst' "$ROOT/test/" "$D/test/"
for x in src tools host fw info.yaml; do ln -sfn "$ROOT/$x" "$D/$x"; done
cp "$NL" "$D/test/gate_level_netlist.v"
echo "commit $(git -C "$ROOT" rev-parse --short HEAD) dirty_tracked=$(git -C "$ROOT" status --porcelain -uno | wc -l | tr -d ' ') clock_hz=$(sed -n 's/^ *clock_hz: *\([0-9]*\).*/\1/p' "$ROOT/info.yaml") gate level${SDF:+ SDF $(basename "$SDF")} start $(date)"
echo "netlist sha256 $( (sha256sum "$NL" 2>/dev/null || shasum -a 256 "$NL") | cut -d' ' -f1)"
if [ -n "${SDF:-}" ]; then
  # Icarus cannot parse the SRAM macro's bus-indexed IOPATHs and aborts the
  # rest of the file; its behavioral model has no specify block, so drop it.
  python3 - "$SDF" "$D/test/annotate.sdf" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
cells = re.split(r"(?=\n \(CELL\n)", text)
keep = [c for c in cells if 'CELLTYPE "RM_IHPSG13' not in c]
open(sys.argv[2], "w").write("".join(keep) + ("" if keep[-1].rstrip().endswith(")") else ")\n"))
print("sdf cells", len(cells) - 1, "kept", len(keep) - 1)
PY
  export SDF=$D/test/annotate.sdf
fi
[ -f "$ROOT/.venv/bin/activate" ] && source "$ROOT/.venv/bin/activate"
cd "$D/test"; B=-B; rc=0
if [ -z "$TESTS" ]; then
  make -B GATES=yes COCOTB_TEST_MODULES=$MODS || rc=1
  grep -q "<failure" results.xml && rc=1
else
  for t in $TESTS; do
    make $B GATES=yes COCOTB_TEST_MODULES=$MODS COCOTB_TEST_FILTER=$t || rc=1; B=
    grep -q "<failure" results.xml && rc=1
  done
fi
exit $rc
