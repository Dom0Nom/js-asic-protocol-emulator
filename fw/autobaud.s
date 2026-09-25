; Autobaud (host image "autobaud"): lane 1 in capture mode on the RX pin.
; T1 timestamps every edge, stores P_N pulse widths (cycles) in SRAM, then
; leaves capture mode and sends them, followed by the lane status word (bit 8:
; the edge FIFO overran while collecting), to the host, and halts. A 30000-cycle watch window marks idle
; gaps: the width across a gap is discarded, so a long idle period can never
; alias (16-bit timestamps wrap every 65536 cycles) into a short pulse.

        .include "tempo.inc"

        .entry 1, main
main:   stop l1
        lc   r0, P_PINS
        ldi  r1, [r0 + 0]
        lw   l1, pins, r1
        ldi  r1, [r0 + 1]
        lw   l1, mode, r1              ; CAPTURE, optional filter
        ldi  r4, [r0 + 2]              ; widths to collect
        ldi  r6, [r0 + 3]              ; buffer
        li   r2, 0                     ; no previous edge yet
loop:   now  r1
        lc   r7, 30000
        add  r1, r1, r7
        li   r7, 1 << 1                ; lane 1 edge FIFO not empty
        wany r1, r7
        cmpi r1, 0
        bne  got
        li   r2, 0                     ; idle gap: forget the previous edge
        bra  loop
got:    rx   r3, l1
        cmpi r2, 0
        beq  first
        sub  r1, r3, r5
        sti  r1, r6
        addi r6, 1
        addi r4, -1
        beq  send
first:  mov  r5, r3
        li   r2, 1
        bra  loop

send:   li   r1, 0
        lw   l1, mode, r1              ; leave capture mode
        wait l1, idle
        stat r7, l1                    ; OVERRUN while collecting?
        ldi  r4, [r0 + 2]
        ldi  r6, [r0 + 3]
s_loop: ldi  r1, [r6 + 0]
        hput r1
        addi r6, 1
        djnz r4, s_loop
        hput r7                        ; lane status: bit 8 = edges were lost
        halt

P_PINS:  .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 1) << 4 | NOPIN
P_MODE:  .word MODE_CAPTURE
P_N:     .word 48
P_BUF:   .word 0x300
