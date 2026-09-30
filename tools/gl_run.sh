#!/usr/bin/env bash
# Gate-level cocotb run against a netlist, in a scratch copy of test/ so it can
# run beside the RTL suite.
# usage: tools/gl_run.sh <netlist.v> <tag> "<test names, space separated>" [modules]
# PDK_ROOT must hold ihp-sg13cmos5l/libs.ref (default: a link to ./pdk).
set -e; unset CDPATH
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
NL=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); TAG=$2; TESTS=$3; MODS=${4:-test,test_usecases}
GL=$ROOT/runs/gl; D=$GL/work_$TAG
if [ -z "${PDK_ROOT:-}" ]; then
  mkdir -p "$GL/pdkroot"; ln -sfn "$ROOT/pdk" "$GL/pdkroot/ihp-sg13cmos5l"; PDK_ROOT=$GL/pdkroot
fi
export PDK_ROOT
rm -rf "$D"; mkdir -p "$D"
rsync -a --exclude sim_build --exclude results.xml --exclude '*.fst' "$ROOT/test/" "$D/test/"
for x in src tools host fw; do ln -sfn "$ROOT/$x" "$D/$x"; done
cp "$NL" "$D/test/gate_level_netlist.v"
shasum -a 256 "$NL"
source "$ROOT/.venv/bin/activate"
cd "$D/test"; B=-B; rc=0
for t in $TESTS; do
  make $B GATES=yes NODUMP=yes COCOTB_TEST_MODULES=$MODS COCOTB_TEST_FILTER=$t || rc=1; B=
done
exit $rc
