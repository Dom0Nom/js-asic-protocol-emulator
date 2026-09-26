; Reset the runtime before loading another protocol image.
; STOP aborts work but deliberately retains lane configuration and pin
; ownership. Clear every writable configuration register, disown pins,
; discard old H2T commands, and send one zero word when finished.

        .include "tempo.inc"
        .entry 0, main
main:   stop l0
        stop l1
        stop l2
        li r0, 0
        li r1, -1
        ioe r0, r1
        iow r0, r1
        st r0, [r0 + 0]
        st r0, [r0 + 1]
        st r0, [r0 + 2]
        st r0, [r0 + 3]
        st r0, [r0 + 4]
        st r0, [r0 + 5]
        st r0, [r0 + 6]
        st r0, [r0 + 7]
        lw l0, mode, r0
        lw l1, mode, r0
        lw l2, mode, r0
        lw l0, cfg, r0
        lw l1, cfg, r0
        lw l2, cfg, r0
        lw l0, frac, r0
        lw l1, frac, r0
        lw l2, frac, r0
        lw l0, cr, r0
        lw l1, cr, r0
        lw l2, cr, r0
        lw l0, cf, r0
        lw l1, cf, r0
        lw l2, cf, r0
        lw l0, samp, r0
        lw l1, samp, r0
        lw l2, samp, r0
        lw l0, pat, r0
        lw l1, pat, r0
        lw l2, pat, r0
        lw l0, pate, r0
        lw l1, pate, r0
        lw l2, pate, r0
        lw l0, crc, r0
        lw l1, crc, r0
        lw l2, crc, r0
        lw l0, poly, r0
        lw l1, poly, r0
        lw l2, poly, r0
        lw l0, sym0, r0
        lw l1, sym0, r0
        lw l2, sym0, r0
        lw l0, sym1, r0
        lw l1, sym1, r0
        lw l2, sym1, r0
        li r1, -1
        lw l0, pins, r1
        lw l1, pins, r1
        lw l2, pins, r1
        li r1, 2
        lw l0, len0, r1
        lw l1, len0, r1
        lw l2, len0, r1
        li r1, 2
        lw l0, len1, r1
        lw l1, len1, r1
        lw l2, len1, r1
        stat r1, l0
        stat r1, l1
        stat r1, l2
drain:  hcnt r1
        andi r1, 255
        beq ready
        hget r1
        bra drain
ready:  hput r0
        halt
