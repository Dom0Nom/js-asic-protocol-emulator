; One Manchester frame on uio1, 1 Mbit/s at 50 MHz.
; Initialize via entry T1, then resume at exported constant mc_run.
        .equ MC_REPEAT, 0
        .include "manchester_core.inc"
