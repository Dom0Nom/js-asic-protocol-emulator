; Firmware-defined Manchester, 50 cycles per bit (500 kbit/s at the
; declared 25 MHz core clock).
; 0 = low/high, 1 = high/low, MSB first, split at 25 of 50 cycles.
; This is a line-code demonstration, not an Ethernet or USB PHY.
; T1 initializes lane 1 on uio1 and halts. Resume at mc_run to send
; A5 96 3C 69, with a queued start time, then return to low idle.
; The including image sets MC_REPEAT (0: one frame, 1: repeat).

        .include "tempo.inc"
        .entry 1, mc_init
mc_init:
        lc   r1, NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | (UIO0 + 1)
        lw   l1, pins, r1
        li   r1, 50
        lw   l1, len0, r1
        lw   l1, len1, r1
        lc   r1, SYM_B | 25
        lw   l1, sym0, r1
        lc   r1, SYM_A | 25
        lw   l1, sym1, r1
        li   r1, CFG_MSB
        lw   l1, cfg, r1
        li   r1, MODE_FRAME
        lw   l1, mode, r1
        li   r1, E_DEN | E_EOF
        emit l1, r1
        wait l1, idle
        halt

K_MC_RUN:
mc_run:
        now  r6
        lc   r1, 1000
        add  r6, r6, r1
        start l1, r6
        lc   r1, 0xA596
        tx   l1, r1, 16
        lc   r1, 0x3C69
        tx   l1, r1, 16
        li   r1, E_DEN | E_EOF
        emit l1, r1
        wait l1, idle
        li   r5, MC_REPEAT
        cmpi r5, 0
        bne  mc_run
        halt
