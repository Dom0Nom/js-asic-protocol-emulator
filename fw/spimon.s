; Passive SPI bus monitor (host image "spimon"). No pin is ever driven.
;   lane 0: I = MOSI, C = SCK, A = CS; lane 1: I = MISO, same C and A.
;           External-clock receive (MODE.XCLK, edge from P_EDGE) of 8-bit
;           words, MSB first; CS rising (stop pattern) cuts a word short.
;   lane 2: I = CS, start pattern CS falling; its CAP stamps each
;           transaction's start (read at the transaction's first byte; a
;           transaction without any SCK edge is not recorded).
; Receives are always queued two ahead as continuations, so a transaction
; is captured from its first SCK edge; bits stay aligned because CS rising
; ends every word (SCK must not toggle while CS is high).
; T0 decodes into a 512-word SRAM ring, T2 forwards it to the host. Only the
; first P_KEEP bytes of each transaction are recorded (0xFFFF: all); the
; rest are counted, which lets the monitor keep up at a higher SCK.
;
; Records (host words):
;   0x8000, time          CS fell at `time`
;   0x0000 | mosi         a MOSI byte   } recorded bytes come in pairs,
;   0x0100 | miso         its MISO byte }
;   0xA000 | bits, count  CS rose; bits of a cut-off byte, total bytes
; Lane 0 or 1 UNDER (host status) means SCK edges arrived with no receive
; queued: the firmware fell behind and bytes were lost.
;   0x9000, time          time marker (30000 cycles with no timed record)
;   0xC000 | lost         ring overflowed

        .include "tempo.inc"
        .equ RING_MASK, 511
        .equ CS_FALL, (PP_A | P_A) << 8 | PP_A
        .equ CS_RISE, (PP_A | P_A) << 8 | P_A
        .equ CS_FALL_I, (PP_I | P_I) << 8 | PP_I

        .entry 0, mon
        .entry 2, pump

mon:    stop l0
        stop l1
        stop l2
        lc   r0, P_MOSI
        ldi  r1, [r0 + 0]
        lw   l0, pins, r1
        ldi  r1, [r0 + 1]              ; P_MISO
        lw   l1, pins, r1
        ldi  r1, [r0 + 2]              ; P_CS
        lw   l2, pins, r1
        ldi  r1, [r0 + 3]              ; P_EDGE: XCLK rising or falling, filter
        lw   l0, mode, r1
        lw   l1, mode, r1
        lc   r1, CS_RISE
        lw   l0, pate, r1
        lw   l1, pate, r1
        lc   r1, CS_FALL_I
        lw   l2, pat, r1
        lc   r1, CFG_MSB | CFG_DREL
        lw   l0, cfg, r1
        lw   l1, cfg, r1
        li   r2, 0                     ; ring write index
        li   r3, 0
        st   r2, [r3 + 0]
        st   r2, [r3 + 1]              ; lost count
        st   r2, [r3 + 2]              ; pump read index
        st   r2, [r3 + 5]              ; bytes in this transaction
        ldi  r6, [r0 + 4]              ; ring base
        wait l0, idle
        wait l1, idle
        wait l2, idle
        stat r1, l2
        rxn  l0, 8, c                  ; one running and, with the loop's,
        rxn  l1, 8, c                  ; two queued per lane: the thread may
        rxn  l0, 8, c                  ; fall two bytes behind without losing
        rxn  l1, 8, c                  ; an SCK edge
        now  r0                        ; r0: time of the last timed record
loop:   rxn  l0, 8, c                  ; always one receive queued ahead
        rxn  l1, 8, c
l_mark: now  r1
        sub  r5, r1, r0
        lc   r3, 30000
        cmp  r5, r3
        bcc  l_wait
        mov  r0, r1
        li   r4, 0
        lih  r4, 0x90
        call put
        mov  r4, r0
        call put
        lc   r3, 30000
l_wait: add  r1, r0, r3
        li   r5, 1                     ; lane 0 has a word
        wany r1, r5
        cmpi r1, 0
        beq  l_mark
        rx   r4, l0                    ; MOSI; C = CS rose inside it
        bcs  w_end
        rx   r5, l1                    ; MISO, same SCK edge
        li   r3, 0
        ld   r1, [r3 + 5]
        addi r1, 1
        st   r1, [r3 + 5]
        cmpi r1, 1
        bne  w_keep
        st   r4, [r3 + 4]              ; first byte: record the CS fall, stamped
        li   r4, 0                     ; by lane 2's CAP (lane 2 never runs a
        lih  r4, 0x80                  ; command, so CAP is the latest fall)
        call put
        lr   r4, l2, cap
        mov  r0, r4                    ; a timed record
        call put
        li   r3, 0
        ld   r4, [r3 + 4]
        li   r1, 1
w_keep: lc   r3, P_KEEP
        ldi  r3, [r3 + 0]
        cmp  r3, r1                    ; keep >= count: record it
        bcc  loop
        andi r4, 0xFF
        call put
        mov  r4, r5
        lih  r4, 0x01
        call put
        bra  loop
w_end:  rx   r5, l1                    ; the MISO half of the cut-off word
        stat r4, l0                    ; bits of the cut-off word, [15:11]
        shri r4, r4, 11
        andi r4, 31
        lih  r4, 0xA0
        call put
        li   r3, 0
        ld   r4, [r3 + 5]              ; bytes in the transaction
        li   r1, 0
        st   r1, [r3 + 5]
        call put
        bra  loop

; store r4 in the ring; clobbers r1, r3
put:    li   r3, 0
        ld   r1, [r3 + 2]              ; pump read index
        addi r2, 1
        lc   r3, RING_MASK
        and  r2, r2, r3
        cmp  r2, r1
        bne  p_ok
        addi r2, -1                    ; full: drop, count the loss
        and  r2, r2, r3
        li   r3, 0
        ld   r1, [r3 + 1]
        addi r1, 1
        st   r1, [r3 + 1]
        ret
p_ok:   addi r2, -1
        and  r2, r2, r3
        add  r1, r6, r2
        sti  r4, r1
        addi r2, 1
        and  r2, r2, r3
        li   r3, 0
        st   r2, [r3 + 0]              ; publish
        ret

; ------------------------------------------------------------ T2: ring -> host
pump:   lc   r0, P_MOSI
        ldi  r6, [r0 + 4]
        li   r2, 0                     ; read index
        li   r5, 0
        lc   r4, RING_MASK
p_wait: ld   r3, [r5 + 1]              ; losses to report first
        cmpi r3, 0
        beq  p_ring
        li   r1, 0
        st   r1, [r5 + 1]
        lih  r3, 0xC0
        hput r3
p_ring: ld   r3, [r5 + 0]
        cmp  r3, r2
        beq  p_wait
        add  r1, r6, r2
        ldi  r1, [r1 + 0]
        hput r1
        addi r2, 1
        and  r2, r2, r4
        st   r2, [r5 + 2]
        bra  p_wait

P_MOSI: .word (UIO0 + 3) << 12 | (UIO0 + 2) << 8 | (UIO0 + 0) << 4 | NOPIN
P_MISO: .word (UIO0 + 3) << 12 | (UIO0 + 2) << 8 | (UIO0 + 1) << 4 | NOPIN
P_CS:   .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 3) << 4 | NOPIN
P_EDGE: .word 1 << 12
P_RING: .word 0x200
P_KEEP: .word 0xFFFF
