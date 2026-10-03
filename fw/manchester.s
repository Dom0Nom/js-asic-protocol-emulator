; One Manchester frame on uio1, 50 cycles per bit (500 kbit/s at 25 MHz).
; Initialize via entry T1, then resume at exported constant mc_run.
        .equ MC_REPEAT, 0
        .include "manchester_core.inc"
