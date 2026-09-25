; JTAG vector engine (host image "jtag"). Two lanes share one timeline:
;   lane 0: TDI (D), TDO (I), TCK (C); lane 1: TMS (D), no clock.
; Each bit slot: TCK low in the first half and rising at P_CR, where the
; target samples TMS and TDI; TDO (changed by the target on the previous
; falling edge) is sampled at P_SAMP. TMS and TDI change at the slot start.
;
; Host words: a header n (1..16 bits), then TMS bits, then TDI bits (LSB
; first). Reply: the n TDO bits, LSB first.
;
; Both lanes' commands for a vector are anchored with `start` at the same
; cycle, so TMS and TDI stay aligned bit for bit. The thread takes the next
; vector only after the previous one's TDO word, so TCK pauses between
; vectors (JTAG allows that). A missed anchor would raise LATE on a lane,
; reported through the host status.

        .include "tempo.inc"

        .entry 0, main
main:   stop l0
        stop l1
        lc   r0, P_PINS
        ldi  r1, [r0 + 0]
        lw   l0, pins, r1
        ldi  r1, [r0 + 1]
        lw   l1, pins, r1
        ldi  r1, [r0 + 2]
        lw   l0, len0, r1
        lw   l0, len1, r1
        lw   l1, len0, r1
        lw   l1, len1, r1
        ldi  r1, [r0 + 3]
        lw   l0, cr, r1
        ldi  r1, [r0 + 4]
        lw   l0, cf, r1
        ldi  r1, [r0 + 5]
        lw   l0, samp, r1
        lc   r1, SYM_NRZ1
        lw   l0, sym1, r1
        lw   l1, sym1, r1
        lc   r1, CFG_CEN
        lw   l0, cfg, r1
        li   r1, 0
        lw   l1, cfg, r1
        li   r1, E_D | E_DEN | E_CEN   ; TDI high, TCK low
        emit l0, r1
        li   r1, E_D | E_DEN           ; TMS high
        emit l1, r1
        wait l0, idle
        wait l1, idle

vec:    hget r3                        ; n
        hget r4                        ; TMS bits
        hget r5                        ; TDI bits
        now  r6
        addi r6, 64                    ; time to queue both starts and both tx (~44 cycles)
        start l0, r6                   ; both lanes begin in the same cycle
        start l1, r6
        ; tx with a run-time bit count: jump into a table of 16 entries
        lc   r1, tab - 3
        add  r1, r1, r3
        add  r1, r1, r3
        add  r1, r1, r3
        jr   r1
tab:    tx   l1, r4, 1
        tx   l0, r5, 1, x
        bra  done
        tx   l1, r4, 2
        tx   l0, r5, 2, x
        bra  done
        tx   l1, r4, 3
        tx   l0, r5, 3, x
        bra  done
        tx   l1, r4, 4
        tx   l0, r5, 4, x
        bra  done
        tx   l1, r4, 5
        tx   l0, r5, 5, x
        bra  done
        tx   l1, r4, 6
        tx   l0, r5, 6, x
        bra  done
        tx   l1, r4, 7
        tx   l0, r5, 7, x
        bra  done
        tx   l1, r4, 8
        tx   l0, r5, 8, x
        bra  done
        tx   l1, r4, 9
        tx   l0, r5, 9, x
        bra  done
        tx   l1, r4, 10
        tx   l0, r5, 10, x
        bra  done
        tx   l1, r4, 11
        tx   l0, r5, 11, x
        bra  done
        tx   l1, r4, 12
        tx   l0, r5, 12, x
        bra  done
        tx   l1, r4, 13
        tx   l0, r5, 13, x
        bra  done
        tx   l1, r4, 14
        tx   l0, r5, 14, x
        bra  done
        tx   l1, r4, 15
        tx   l0, r5, 15, x
        bra  done
        tx   l1, r4, 16
        tx   l0, r5, 16, x
done:   rx   r1, l0
        hput r1
        bra  vec

P_PINS:  .word NOPIN << 12 | (UIO0 + 0) << 8 | (UIO0 + 3) << 4 | (UIO0 + 2)
P_TMS:   .word NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | (UIO0 + 1)
P_PER:   .word 20
P_CR:    .word 10
P_CF:    .word 20
P_SAMP:  .word 10
