; Logic analyser (host image "la"). Lanes 0-2 capture edges on up to three
; pins (capture mode: every edge of the lane's I pin pushes its timestamp);
; lane 0's A pin can carry a trigger (its start pattern stays active in
; capture mode). One recording thread per channel
; (T0-T2, each with its own SRAM region), so channels never slow each other;
; T3 arms, triggers, stops and reports.
;
; Record word: [15:14] the new level (0 or 1), [13:0] low 14 bits of the
; edge's cycle. Code 3 is a time marker ([13:0] = low bits of NOW), written
; when a channel has had no record for 4096 cycles, so consecutive records
; of a channel are less than 8192 cycles apart and the host can rebuild full
; timestamps. Each channel has its own region. The header's levels (read
; with ior just after capture starts) only matter for a channel without
; edges.
;
; Each recorder leaves capture mode on its lane as soon as it has finished.
;
; Host: write one word to arm. With P_TRIG != 0, T3 waits for lane 0's start
; pattern (P_TRIGPAT, on the A pin of P_PINS0) before capturing. Capture ends when
; every channel has stored P_N records, or when the host writes a second word
; (recorders notice within 4096 cycles). Reply: records stored per channel
; (3 words, bit 15 set if that channel's edge FIFO overran), header (initial
; levels in [2:0]), start time.
;
; Scratch: [0] start generation, [1] header, [2] start time, [3..5] records
; per channel (0xFFFF while running), [6] stop flag.

        .include "tempo.inc"

        .entry 0, rec0
        .entry 1, rec1
        .entry 2, rec2
        .entry 3, ctl

; ---- recording threads: one per lane. r0 wany mask, r2 = 4096, r4 records
; left, r5 channel, r6 write pointer, r7 marker deadline (now + 4096 after
; each marker; edges do not move it, so a busy channel also gets a marker
; about every 4096 cycles, which keeps the loop short).
rec0:   li   r0, 1                     ; wany mask: this lane's edge FIFO
        call setup
l0_lp:  mov  r1, r7
        wany r1, r0
        cmpi r1, 0
        beq  l0_mk
        rx   r3, l0                     ; N flag = the new level
        bpl  l0_lo
        shli r3, r3, 2                 ; [15:14] = 1: rising edge
        ori  r3, 1
        rori r3, r3, 2
        bra  l0_st
l0_lo:  shli r3, r3, 2                 ; [15:14] = 0: falling edge
        shri r3, r3, 2
l0_st:  sti  r3, r6
        addi r6, 1
        addi r4, -1
        bne  l0_lp
        bra  fin
l0_mk:  li   r1, 0                     ; timeout: stop requested?
        ld   r1, [r1 + 6]
        cmpi r1, 0
        bne  fin
        now  r3                        ; else a time marker
        add  r7, r3, r2                ; next deadline: now + 4096
        shli r3, r3, 2
        ori  r3, 3
        rori r3, r3, 2
        sti  r3, r6
        addi r6, 1
        addi r4, -1
        bne  l0_lp
        bra  fin

rec1:   li   r0, 2
        call setup
l1_lp:  mov  r1, r7
        wany r1, r0
        cmpi r1, 0
        beq  l1_mk
        rx   r3, l1                     ; N flag = the new level
        bpl  l1_lo
        shli r3, r3, 2                 ; [15:14] = 1: rising edge
        ori  r3, 1
        rori r3, r3, 2
        bra  l1_st
l1_lo:  shli r3, r3, 2                 ; [15:14] = 0: falling edge
        shri r3, r3, 2
l1_st:  sti  r3, r6
        addi r6, 1
        addi r4, -1
        bne  l1_lp
        bra  fin
l1_mk:  li   r1, 0                     ; timeout: stop requested?
        ld   r1, [r1 + 6]
        cmpi r1, 0
        bne  fin
        now  r3                        ; else a time marker
        add  r7, r3, r2                ; next deadline: now + 4096
        shli r3, r3, 2
        ori  r3, 3
        rori r3, r3, 2
        sti  r3, r6
        addi r6, 1
        addi r4, -1
        bne  l1_lp
        bra  fin

rec2:   li   r0, 4
        call setup
l2_lp:  mov  r1, r7
        wany r1, r0
        cmpi r1, 0
        beq  l2_mk
        rx   r3, l2                     ; N flag = the new level
        bpl  l2_lo
        shli r3, r3, 2                 ; [15:14] = 1: rising edge
        ori  r3, 1
        rori r3, r3, 2
        bra  l2_st
l2_lo:  shli r3, r3, 2                 ; [15:14] = 0: falling edge
        shri r3, r3, 2
l2_st:  sti  r3, r6
        addi r6, 1
        addi r4, -1
        bne  l2_lp
        bra  fin
l2_mk:  li   r1, 0                     ; timeout: stop requested?
        ld   r1, [r1 + 6]
        cmpi r1, 0
        bne  fin
        now  r3                        ; else a time marker
        add  r7, r3, r2                ; next deadline: now + 4096
        shli r3, r3, 2
        ori  r3, 3
        rori r3, r3, 2
        sti  r3, r6
        addi r6, 1
        addi r4, -1
        bne  l2_lp
        bra  fin

; wait for the start, then set up this channel's pointers; clobbers r1, r3
setup:  mov  r5, r7                    ; keep the return address
        li   r2, 0
        ld   r3, [r2 + 0]              ; generation before arming
        tid  r7                        ; channel = thread number
        lc   r1, P_N0
        add  r1, r1, r7
        ldi  r4, [r1 + 0]              ; records left (P_N0 + channel)
        lc   r1, P_BUF
        ldi  r6, [r1 + 0]
        ldi  r1, [r1 + 1]              ; P_STRIDE
        mov  r2, r7
s_off:  cmpi r2, 0
        beq  s_wait
        add  r6, r6, r1
        addi r2, -1
        bra  s_off
s_wait: ld   r1, [r2 + 0]              ; T3 bumps the generation to start
        cmp  r1, r3
        beq  s_wait
        mov  r1, r5                    ; return address
        tid  r5
        lc   r2, 4096
        now  r7
        add  r7, r7, r2
        cmpi r4, 0
        beq  fin                       ; unused channel
        jr   r1

; finish: leave capture mode on this lane, then store records | overrun << 15
fin:    li   r7, 0
        cmpi r5, 1
        beq  fin1
        bcc  fin0
        lw   l2, mode, r7
        wait l2, idle
        stat r3, l2
        bra  fin_n
fin0:   lw   l0, mode, r7
        wait l0, idle
        stat r3, l0
        bra  fin_n
fin1:   lw   l1, mode, r7
        wait l1, idle
        stat r3, l1
fin_n:  shri r3, r3, 8
        andi r3, 1
        rori r3, r3, 1                 ; overrun -> bit 15
        lc   r1, P_N0
        add  r1, r1, r5
        ldi  r1, [r1 + 0]
        sub  r1, r1, r4                ; records stored
        or   r1, r1, r3
        li   r3, 0
        add  r3, r3, r5
        st   r1, [r3 + 3]
        halt

; ---- control thread
ctl:    stop l0
        stop l1
        stop l2
        li   r7, 0
        li   r1, -1
        st   r1, [r7 + 3]
        st   r1, [r7 + 4]
        st   r1, [r7 + 5]
        li   r1, 0
        st   r1, [r7 + 6]
        lc   r0, P_PINS0
        ldi  r1, [r0 + 0]
        lw   l0, pins, r1
        ldi  r1, [r0 + 1]
        lw   l1, pins, r1
        ldi  r1, [r0 + 2]
        lw   l2, pins, r1
        ldi  r1, [r0 + 4]
        lw   l0, pat, r1
        hget r1                        ; arm
        ldi  r1, [r0 + 5]              ; P_TRIG
        cmpi r1, 0
        beq  go
        wait l0, idle
        stat r1, l0                    ; forget matches from before arming
        wait l0, pat                   ; trigger: lane 0's start pattern
go:     ldi  r1, [r0 + 6]              ; P_MODE: capture, filter
        lw   l0, mode, r1
        lw   l1, mode, r1
        lw   l2, mode, r1
        wait l0, idle                  ; capture mode is on from here
        wait l1, idle
        wait l2, idle
        stat r1, l0                    ; clear stale flags
        stat r1, l1
        stat r1, l2
        now  r5
        ior  r1                        ; initial levels
        li   r3, 0
        ldi  r4, [r0 + 8]              ; input bit of channel 0
        tst  r1, r4
        beq  lv1
        ori  r3, 1
lv1:    ldi  r4, [r0 + 9]
        tst  r1, r4
        beq  lv2
        ori  r3, 2
lv2:    ldi  r4, [r0 + 10]
        tst  r1, r4
        beq  lvd
        ori  r3, 4
lvd:    st   r3, [r7 + 1]              ; header
        st   r5, [r7 + 2]              ; start time
        ld   r1, [r7 + 0]
        addi r1, 1
        st   r1, [r7 + 0]              ; start the recorders
c_wait: now  r1
        addi r1, 255
        lc   r2, 1 << 8                ; host stop word
        wany r1, r2
        cmpi r1, 0
        bne  c_stop
        ld   r1, [r7 + 3]              ; every channel done?
        cmpi r1, -1
        beq  c_wait
        ld   r1, [r7 + 4]
        cmpi r1, -1
        beq  c_wait
        ld   r1, [r7 + 5]
        cmpi r1, -1
        beq  c_wait
        bra  c_done
c_stop: hget r1
        li   r1, 1
        st   r1, [r7 + 6]
c_all:  ld   r1, [r7 + 3]              ; recorders stop within 4096 cycles
        cmpi r1, -1
        beq  c_all
        ld   r1, [r7 + 4]
        cmpi r1, -1
        beq  c_all
        ld   r1, [r7 + 5]
        cmpi r1, -1
        beq  c_all
c_done: ld   r3, [r7 + 1]              ; header
        hcnt r1                        ; take a late stop word
        andi r1, 7
        beq  c_rep
        hget r1
c_rep:  ld   r1, [r7 + 3]
        hput r1
        ld   r1, [r7 + 4]
        hput r1
        ld   r1, [r7 + 5]
        hput r1
        hput r3
        ld   r1, [r7 + 2]
        hput r1
        halt

P_PINS0:   .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 0) << 4 | NOPIN
P_PINS1:   .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 1) << 4 | NOPIN
P_PINS2:   .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 2) << 4 | NOPIN
P_UNUSED3: .word 0
P_TRIGPAT: .word (PP_A | P_A) << 8 | PP_A
P_TRIG:    .word 0
P_MODE:    .word MODE_CAPTURE
P_UNUSED:  .word 0
P_BIT0:    .word 1
P_BIT1:    .word 2
P_BIT2:    .word 4
P_N0:      .word 160
P_N1:      .word 160
P_N2:      .word 160
P_BUF:     .word 0x200
P_STRIDE:  .word 160
