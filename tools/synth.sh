#!/usr/bin/env bash
# Quick local synthesis against the sg13cmos5l typ liberty (copied to pdk/ from
# the PC, see notes/progress.md). Prints cell count and area for one top module.
# usage: tools/synth.sh <top> [chparam args...]  (sources: src/*.v minus project.v)
set -euo pipefail
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
LIB=$ROOT/pdk/libs.ref/sg13cmos5l_stdcell/lib/sg13cmos5l_stdcell_typ_1p20V_25C.lib
TOP=$1; shift
SRCS=$({ ls "$ROOT"/src/*.v "$ROOT"/src/macros/*.bb.v 2>/dev/null || true; } | grep -v project.v | tr '\n' ' ')
LOG=$(mktemp -t synth).log
yosys -q -l "$LOG" -p "
  read_verilog -I$ROOT/src $SRCS
  chparam $* $TOP
  synth -flatten -top $TOP
  dfflibmap -liberty $LIB
  abc -liberty $LIB
  opt_clean
  tee -o $LOG.stat stat -liberty $LIB
" >/dev/null
grep -iE "error" "$LOG" | head -5 || true
if grep -qE "Latch inferred|conflicting drivers" "$LOG"; then
  echo "SYNTH CHECK FAILED: latch or multiple drivers"; grep -E "Latch inferred|conflicting drivers" "$LOG" | head -5; exit 1
fi
awk '/Number of cells|cells$/ && !done {print} /Chip area/ {print; done=1}' "$LOG.stat"
grep -E "dfrbp|dlh" "$LOG.stat" | awk '{s+=$1} END {print "flops/latches:", s}'
