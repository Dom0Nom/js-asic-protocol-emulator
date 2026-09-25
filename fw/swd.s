; SWD host on lane 0 (host image "swd"). SWDIO is the lane's D and I pin
; (released in receive slots and when idle, CFG.DREL), SWCLK its C pin.
; Each bit slot: SWCLK low in the first half, rising at P_CR; the host
; changes SWDIO at the slot start and samples at P_SAMP, before the rising
; edge on which the target changes SWDIO (ADIv5.2 B4.3.1, docs/specs.md 5.1).
;
; Host words:
;   0x1000 | n  SEQ: n bits (1..4095) driven from the following words, LSB
;               first (line reset, JTAG-to-SWD, idle cycles)
;   0x2000 | R  READ with request byte R: reply {parity error << 8 | ACK};
;               if ACK is OK two more words: data[15:0], data[31:16]
;   0x3000 | R  WRITE with request byte R, then data[15:0], data[31:16]
;               from the host: reply ACK. Data is sent only after an OK ACK
;               (a WAIT or FAULT is followed by the turnaround only).
; ACK values (LSB first on the wire): 1 OK, 2 WAIT, 4 FAULT, others error.

        .include "tempo.inc"

        .entry 0, main
main:   stop l0
        lc   r0, P_PINS
        ldi  r1, [r0 + 0]
        lw   l0, pins, r1
        ldi  r1, [r0 + 1]
        lw   l0, len0, r1
        lw   l0, len1, r1
        ldi  r1, [r0 + 2]
        lw   l0, cr, r1
        ldi  r1, [r0 + 3]
        lw   l0, cf, r1
        ldi  r1, [r0 + 4]
        lw   l0, samp, r1
        lc   r1, SYM_NRZ1
        lw   l0, sym1, r1
        lc   r1, CFG_CEN | CFG_DREL
        lw   l0, cfg, r1
        li   r2, 0xFF

cmd:    hget r4
        shri r5, r4, 12
        lc   r1, 0x0FFF
        and  r4, r4, r1
        cmpi r5, 1
        beq  c_seq
        cmpi r5, 2
        beq  c_read
        cmpi r5, 3
        beq  c_write
        bra  cmd

; ---- SEQ n
c_seq:  cmpi r4, 16
        bcc  s_tail                    ; fewer than 16 left
        hget r5
        tx   l0, r5, 16
        addi r4, -16
        bra  c_seq
s_tail: cmpi r4, 0
        beq  cmd
        hget r5                        ; 1..15 bits: one per slot
s_bit:  tx   l0, r5, 1
        shri r5, r5, 1
        djnz r4, s_bit
        bra  cmd

; ---- request phase and ACK; r4 = request byte. r3 = ACK; clobbers r1
req:    and  r4, r4, r2
        tx   l0, r4, 8
        rxn  l0, 4                     ; turnaround + 3 ACK bits
        rx   r1, l0
        shri r3, r1, 1
        andi r3, 7
        ret

c_read: call req
        cmpi r3, 1
        bne  r_nak
        rxn  l0, 16                    ; data[15:0]
        rxn  l0, 16                    ; data[31:16] (queued behind the first)
        rx   r5, l0
        rxn  l0, 2                     ; parity + turnaround, queued in time
        rx   r6, l0                    ; so SWCLK runs without a pause
        rx   r1, l0
        xor  r7, r5, r6
        par  r7, r7
        xor  r7, r7, r1
        andi r7, 1
        shli r7, r7, 8
        or   r3, r3, r7
        hput r3
        hput r5
        hput r6
        bra  cmd
r_nak:  rxn  l0, 1                     ; turnaround back to the host
        rx   r1, l0
        hput r3
        bra  cmd

c_write:
        hget r5
        hget r6
        call req
        rxn  l0, 1                     ; turnaround back to the host
        rx   r1, l0
        hput r3
        cmpi r3, 1
        bne  cmd
        tx   l0, r5, 16
        tx   l0, r6, 16
        xor  r7, r5, r6
        par  r7, r7
        tx   l0, r7, 1
        bra  cmd

P_PINS: .word NOPIN << 12 | (UIO0 + 1) << 8 | (UIO0 + 0) << 4 | (UIO0 + 0)
P_PER:  .word 20
P_CR:   .word 10
P_CF:   .word 20
P_SAMP: .word 9
