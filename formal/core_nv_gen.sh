# Write tempo_core_nv.v: tempo_core.v with core_nv.vh included before endmodule.
# Run by yosys `exec` in formal/core_cover.sby (shell, since yosys's bundled
# Python environment is not usable from exec).
set -e
[ "$(grep -c '^endmodule' "$1")" = 1 ]
sed '/^endmodule/i\
`include "core_nv.vh"
' "$1" > "$2"
grep -q 'include "core_nv.vh"' "$2"
