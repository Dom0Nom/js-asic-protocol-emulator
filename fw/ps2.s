; PS/2 device and host, device-to-host direction only.
; Needs PS2_CLK_KHZ, the core clock in kHz (25000 at the declared 25 MHz).
; Frame: start 0, 8 data bits LSB first, odd parity, stop 1.
;
; Thread 0, device (keyboard or mouse side) on lane 2: DATA uio0, CLK uio1,
; both open drain with external pull-ups. One host word per byte ([7:0]).
; The lane generates the clock: 80 us per bit (12.5 kHz), DATA changes
; 20 us before CLK falls, CLK low for 40 us. Frames queued together follow
; each other without a gap.
;
; Thread 1, host on lane 1: DATA uio2, CLK uio3, inputs only. External-clock
; receive (MODE.XCLK falling) takes DATA on each falling CLK edge. One word
; to the host per frame:
;   [7:0] data, [8] parity error, [9] start bit high, [10] stop bit low
;
; Not implemented: host-to-device bytes (request-to-send) in either role,
; the device giving way to a host that holds CLK low, and a receive timeout
; (a frame cut short leaves the receiver out of step with the frames after it).

        .include "tempo.inc"
        .equ PS2_SLOT, PS2_CLK_KHZ * 80 / 1000
        .equ XCLK_FALL, 2 << 12
        .equ STOP_BIT, 1 << 10

        .entry 0, ps2_dev
ps2_dev:
        lc   r1, NOPIN << 12 | (UIO0 + 1) << 8 | NOPIN << 4 | (UIO0 + 0)
        lw   l2, pins, r1
        lc   r1, PS2_SLOT
        lw   l2, len0, r1
        lw   l2, len1, r1
        lc   r1, SYM_NRZ1
        lw   l2, sym1, r1
        lc   r1, PS2_SLOT / 4
        lw   l2, cr, r1                ; CLK falls
        lc   r1, PS2_SLOT * 3 / 4
        lw   l2, cf, r1                ; CLK rises
        lc   r1, CFG_DOD | CFG_COD | CFG_CEN | CFG_CPOL
        lw   l2, cfg, r1
        lc   r4, STOP_BIT
d_loop: hget r2
        andi r2, 0xFF
        par  r3, r2
        xori r3, 1                     ; odd parity
        shli r3, r3, 9
        shli r2, r2, 1                 ; start bit 0 at bit 0
        or   r2, r2, r3
        or   r2, r2, r4
        tx   l2, r2, 11
        bra  d_loop

        .entry 1, ps2_host
ps2_host:
        lc   r1, NOPIN << 12 | (UIO0 + 3) << 8 | (UIO0 + 2) << 4 | NOPIN
        lw   l1, pins, r1
        lc   r1, XCLK_FALL
        lw   l1, mode, r1
        lc   r3, STOP_BIT
        rxn  l1, 11                    ; keep one receive queued ahead
h_loop: rxn  l1, 11
        rx   r4, l1
        xor  r4, r4, r3                ; bit 10: stop bit low
        shli r5, r4, 6
        shri r5, r5, 7                 ; data and parity in [8:0]
        par  r6, r5
        xori r6, 1                     ; even count of ones: parity error
        shli r6, r6, 8
        andi r5, 0xFF
        or   r5, r5, r6
        and  r6, r4, r3
        or   r5, r5, r6
        andi r4, 1                     ; start bit high
        shli r4, r4, 9
        or   r5, r5, r4
        hput r5
        bra  h_loop
