; Passive I2C bus monitor (host image "i2cmon"). No pin is ever driven.
;   lane 0: I = SDA, C = SCL (input). External-clock receive (MODE.XCLK
;           rising): 9-bit words, 8 data bits and the ACK bit, sampled on
;           SCL rising edges. Its stop pattern is START, so a START or
;           repeated START cuts the current word short; the next receive is
;           always queued, so the bits after the START land on a fresh word.
;           (A repeated START's own SCL rise lands in the cut-off word.)
;   lane 1: same pins, patterns only: start pattern START (CAP stamps it),
;           stop pattern STOP.
; T0 decodes into a 512-word SRAM ring, T2 forwards the ring to the host
; (the same ring and pump as the UART console image).
;
; Records (host words):
;   0x0000 | ack << 8 | data   a byte and its ACK bit (1 = NACK)
;   0x8000, time               START or repeated START at `time` (16-bit)
;   0xA000                     STOP (reported at the next word boundary, or
;                              at the next marker when the bus goes quiet)
;   0x9000, time               time marker whenever 30000 cycles pass with no
;                              timed record, so the host can unwrap times
;   0xC000 | lost              ring overflowed; lost records

        .include "tempo.inc"
        .equ RING_MASK, 511
        .equ SCL_RISE, 1 << 12             ; MODE.XCLK: sample on C rising
        ; {prev A,C,I,D, cur A,C,I,D}: I = SDA, C = SCL
        .equ START_PAT, (PP_C | PP_I | P_C | P_I) << 8 | (PP_C | PP_I | P_C)
        .equ STOP_PAT,  (PP_C | PP_I | P_C | P_I) << 8 | (PP_C | P_C | P_I)

        .entry 0, mon
        .entry 2, pump

mon:    stop l0
        stop l1
        lc   r0, P_PINS
        ldi  r1, [r0 + 0]
        lw   l0, pins, r1
        lw   l1, pins, r1
        ldi  r1, [r0 + 1]              ; P_MODE: filter
        lw   l1, mode, r1
        lc   r2, SCL_RISE
        or   r1, r1, r2
        lw   l0, mode, r1
        lc   r1, START_PAT
        lw   l0, pat, r1
        lw   l0, pate, r1
        lw   l1, pat, r1
        lc   r1, STOP_PAT
        lw   l1, pate, r1
        lc   r1, CFG_MSB | CFG_AUTOSTART | CFG_DREL
        lw   l0, cfg, r1
        li   r2, 0                     ; ring write index
        li   r3, 0
        st   r2, [r3 + 0]
        st   r2, [r3 + 1]              ; lost count
        st   r2, [r3 + 2]              ; pump read index
        ldi  r6, [r0 + 2]              ; ring base
        wait l0, idle
        wait l1, idle
        stat r1, l0
        stat r1, l1
        rxn  l0, 9                     ; the first word waits for START
        now  r0                        ; r0: time of the last timed record
loop:   rxn  l0, 9, c                  ; always one receive queued ahead
l_mark: now  r1                        ; 30000 cycles without a timed record:
        sub  r5, r1, r0                ; write a marker (busy or silent)
        lc   r3, 30000
        cmp  r5, r3
        bcc  l_wait
        mov  r0, r1
        call evq                       ; e.g. a STOP before a quiet bus
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
        rx   r4, l0                    ; C = cut short by a START
        bcs  w_cut
        li   r3, 0
        st   r4, [r3 + 4]              ; keep the word across evq
        call evq                       ; the START before the first word
        li   r3, 0
        ld   r4, [r3 + 4]
        mov  r5, r4                    ; data << 1 | ack  ->  ack << 8 | data
        shri r4, r4, 1
        andi r4, 0xFF
        andi r5, 1
        shli r5, r5, 8
        or   r4, r4, r5
        call put
        bra  loop
w_cut:  call evq                       ; STOP then START, or a repeated START
        bra  loop

; record what lane 1 saw since the last look: STOP first, then START with
; its CAP. One stat read (read-and-clear) gives both.
evq:    li   r3, 0
        st   r7, [r3 + 3]              ; keep the link across the puts
        stat r5, l1                    ; bit 4 stop match, bit 3 start match
        li   r1, 1 << 4
        tst  r5, r1
        beq  ev_st
        li   r4, 0
        lih  r4, 0xA0
        call put
ev_st:  li   r1, 1 << 3
        tst  r5, r1
        beq  ev_ret
        li   r4, 0
        lih  r4, 0x80
        call put
        lr   r4, l1, cap
        mov  r0, r4                    ; a timed record
        call put
ev_ret: li   r3, 0
        ld   r7, [r3 + 3]
        ret

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
pump:   lc   r0, P_PINS
        ldi  r6, [r0 + 2]
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

P_PINS: .word NOPIN << 12 | (UIO0 + 5) << 8 | (UIO0 + 4) << 4 | NOPIN
P_MODE: .word 0
P_RING: .word 0x200
