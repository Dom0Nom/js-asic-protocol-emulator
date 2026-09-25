; UART console bridge (host image "uart").
;   T0: host -> TX line on lane 0
;   T1: RX line on lane 1 -> ring buffer in SRAM (sti)
;   T2: ring buffer -> host mailbox
; The ring (512 words at P_RING) absorbs host stalls: about 44 ms of
; continuous traffic at 115200 baud.
;
; Frame format, polarity and bit time come from the parameter block, patched
; by the host (host/tempo_host/uart.py). The transmit and receive bit counts
; are immediates, so the host patches the instructions at P_TXOP and P_RXOP.
;
; Host -> chip: a header word n (characters), then the characters, two per
; word (first in the low byte) for up to 8 data bits, one per word for 9.
; Chip -> host, one word per received character:
;   [8:0] data, [9] framing error (stop bit low), [10] parity error,
;   [11] break (all bits low), [15] ring overflowed before this character

        .include "tempo.inc"
        .equ RING_MASK, 511

        .entry 0, tx_main
        .entry 1, rx_main
        .entry 2, pump

; ------------------------------------------------------------ T0: transmit
tx_main:
        stop l0
        lc   r0, P_TXPINS
        ldi  r1, [r0 + 0]
        lw   l0, pins, r1
        ldi  r1, [r0 + 2]
        lw   l0, len0, r1
        lw   l0, len1, r1
        ldi  r1, [r0 + 3]
        lw   l0, frac, r1
        ldi  r1, [r0 + 5]
        lw   l0, sym0, r1
        ldi  r1, [r0 + 6]
        lw   l0, sym1, r1
        ldi  r1, [r0 + 16]
        lw   l0, mode, r1
        ldi  r1, [r0 + 7]              ; idle level
        emit l0, r1
t_cmd:  hget r4                        ; characters to send
t_loop: cmpi r4, 0
        beq  t_cmd
        hget r5
        mov  r2, r5
        call frame
        addi r4, -1
        beq  t_cmd
        shri r2, r5, 8
        call frame
        addi r4, -1
        bra  t_loop

; send character r2; clobbers r1, r3, r6
frame:  ldi  r3, [r0 + 10]             ; data mask
        and  r2, r2, r3
        shli r1, r2, 1                 ; start bit 0 at bit 0
        ldi  r3, [r0 + 11]             ; parity: 0 none, 1 even, 2 odd
        cmpi r3, 0
        beq  f_stop
        par  r6, r2
        cmpi r3, 2
        bne  f_even
        xori r6, 1
f_even: ldi  r3, [r0 + 12]             ; parity bit position
        shl  r6, r6, r3
        or   r1, r1, r6
f_stop: ldi  r3, [r0 + 14]             ; stop bits, already in place
        or   r1, r1, r3
P_TXOP: tx   l0, r1, 10
        ret

; ------------------------------------------------------------ T1: receive
rx_main:
        stop l1
        lc   r0, P_TXPINS
        ldi  r1, [r0 + 1]
        lw   l1, pins, r1
        ldi  r1, [r0 + 2]
        lw   l1, len0, r1
        ldi  r1, [r0 + 3]
        lw   l1, frac, r1
        ldi  r1, [r0 + 4]
        lw   l1, samp, r1
        ldi  r1, [r0 + 8]
        lw   l1, pat, r1
        ldi  r1, [r0 + 16]
        lw   l1, mode, r1
        lc   r1, CFG_AUTOSTART
        lw   l1, cfg, r1
        li   r2, 0                     ; ring write index
        li   r3, 0
        st   r2, [r3 + 0]
        st   r2, [r3 + 1]              ; overflow flag
        st   r2, [r3 + 2]              ; pump read index
        ldi  r6, [r0 + 15]             ; ring base
        call rxarm                     ; keep one receive queued ahead
r_loop: call rxarm
        rx   r4, l1
        ldi  r1, [r0 + 9]              ; inversion mask
        xor  r4, r4, r1
        li   r7, 0                     ; flags
        ldi  r1, [r0 + 13]             ; stop bit position
        shr  r3, r4, r1
        andi r3, 1
        bne  r_stopok
        li   r7, 1 << 1                ; framing error, bit 9 after the shift below
r_stopok:
        shri r5, r4, 1
        ldi  r1, [r0 + 10]
        and  r5, r5, r1                ; data
        ldi  r1, [r0 + 11]
        cmpi r1, 0
        beq  r_nopar
        par  r3, r5
        cmpi r1, 2
        bne  r_even
        xori r3, 1
r_even: ldi  r1, [r0 + 12]
        shr  r1, r4, r1
        xor  r3, r3, r1
        andi r3, 1
        beq  r_nopar
        ori  r7, 1 << 2                ; parity error
r_nopar:
        cmpi r5, 0                     ; break: all data low with a framing error
        bne  r_out
        li   r1, 1 << 1
        tst  r7, r1
        beq  r_out
        ori  r7, 1 << 3
r_out:  shli r7, r7, 8
        or   r5, r5, r7
        ; ring full? (write index + 1 == read index)
        li   r3, 0
        ld   r1, [r3 + 2]
        addi r2, 1
        lc   r3, RING_MASK
        and  r2, r2, r3
        cmp  r2, r1
        bne  r_store
        addi r2, -1                    ; drop the character, remember the loss
        and  r2, r2, r3
        li   r3, 0
        li   r1, 1
        st   r1, [r3 + 1]
        bra  r_loop
r_store:
        li   r3, 0
        ld   r1, [r3 + 1]              ; mark the first character after a loss
        cmpi r1, 0
        beq  r_put
        lc   r1, 0x8000
        or   r5, r5, r1
        li   r1, 0
        st   r1, [r3 + 1]
r_put:  addi r2, -1
        lc   r3, RING_MASK
        and  r2, r2, r3
        add  r1, r6, r2
        sti  r5, r1
        addi r2, 1
        and  r2, r2, r3
        li   r3, 0
        st   r2, [r3 + 0]              ; publish
        bra  r_loop

rxarm:
P_RXOP: rxn  l1, 10
        ret

; ------------------------------------------------------------ T2: ring -> host
pump:   lc   r0, P_TXPINS
        ldi  r6, [r0 + 15]
        li   r2, 0                     ; read index
        li   r5, 0
        lc   r4, RING_MASK
p_wait: ld   r3, [r5 + 0]
        cmp  r3, r2
        beq  p_wait
        add  r1, r6, r2
        ldi  r1, [r1 + 0]
        hput r1
        addi r2, 1
        and  r2, r2, r4
        st   r2, [r5 + 2]
        bra  p_wait

P_TXPINS:  .word NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | (UIO0 + 0)
P_RXPINS:  .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 1) << 4 | NOPIN
P_LEN:     .word 434
P_FRAC:    .word 0
P_SAMP:    .word 217
P_SYM0:    .word SYM_NRZ0
P_SYM1:    .word SYM_NRZ1
P_IDLE:    .word E_D | E_DEN
P_PAT:     .word (PP_I | P_I) << 8 | PP_I
P_XOR:     .word 0
P_DMASK:   .word 0xFF
P_PARITY:  .word 0
P_PARPOS:  .word 9
P_STOPPOS: .word 9
P_STOPMASK: .word 1 << 9
P_RING:    .word 0x200
P_MODE:    .word 0
