; Passive SPI bus monitor (host image "spimon"). No pin is ever driven.
;   lane 0: I = MOSI, C = SCK, A = CS; lane 1: I = MISO, same C and A.
;           External-clock receive (MODE.XCLK, edge from P_EDGE) of 16-bit
;           words, MSB first; CS rising (stop pattern) cuts a word short.
;   lane 2: I = CS, start pattern CS falling; its CAP stamps each
;           transaction's start.
; T0 pops both lanes: the MOSI and MISO words of one SCK edge complete in
; the same cycle, so they are stored as a pair (MOSI at ring slot 2k, MISO
; at 2k + 1). The firmware touches only whole 16-bit words, about 37 slots
; (148 cycles) per word; the lanes keep two receives queued, so the thread
; may fall two words behind. If it falls further the lane raises UNDER.
; Transaction events go to a small event ring together with the ring slot
; they belong to; T2 merges both rings, in order, into the host mailbox.
; With P_KEEP = K only the first K words of a transaction are stored (the
; cut-off word always is); the rest are counted.
;
; Host words:
;   0xD000 | n, then n pairs (MOSI word, MISO word), n <= 15
;   0x8000, time          CS fell at `time`; the next pair is its first
;   0xA000 | e << 5 | s << 4 | bits, count
;                         CS rose; the last pair was cut off with `bits`
;                         bits, right-aligned (0: no partial pair); `count`
;                         words in the transaction; e = STAT[8:6] (OVERRUN,
;                         STUFFERR, UNDER) of lanes 0 and 1 ORed, which this
;                         firmware's `stat` clears, so it is forwarded here;
;                         s = the MISO lane ended out of step with the MOSI
;                         lane (a lane missed SCK edges), so this
;                         transaction's MISO bytes are not aligned
;   0x9000, time          time marker; timed records are < 16384 cycles apart.
;                         The first one says both lanes are armed.
;   0xC000 | n            n pairs or events lost because a ring was full
; Bit 8 of a 0x8000 or 0xA000 word: events were dropped just before this
; one (the event ring was full), so the open transaction is incomplete.
;
; Scratch: [0] ring write slot (T0), [1] P_KEEP, [2] ring read slot (T2),
; [3] event write index (T0), [4] event read index (T2), [5] losses (T0),
; [6] marker deadline, [7] T0 temporary.

        .include "tempo.inc"
        .equ CS_RISE, (PP_A | P_A) << 8 | P_A
        .equ CS_FALL_I, (PP_I | P_I) << 8 | PP_I
        .equ MARK, 16384
        .equ EVBUF, 0x1B0              ; 16 events of 4 words
        .equ TMP, 0x1F0                ; one spare word
        .equ EARLY, 0x1F1              ; nonzero: lane 1's END came in a pair
        .equ M1W, 0x1F2                ; lane 1's END word at CS rise

        .entry 0, mon
        .entry 2, pump

; ---------------------------------------------------------------- T0
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
        ldi  r6, [r0 + 4]              ; r6: P_RING
        li   r3, 0                     ; r3: 0, the scratch base
        ldi  r1, [r0 + 5]              ; P_KEEP
        st   r1, [r3 + 1]
        st   r3, [r3 + 0]
        st   r3, [r3 + 2]
        st   r3, [r3 + 3]
        st   r3, [r3 + 4]
        st   r3, [r3 + 5]
        lc   r1, EARLY
        sti  r3, r1
        wait l0, idle
        wait l1, idle
        wait l2, idle
        stat r1, l0
        stat r1, l1
        stat r1, l2
        now  r1
        lc   r0, MARK
        add  r1, r1, r0
        st   r1, [r3 + 6]
        sev  0                         ; T2 may start
        li   r2, 0                     ; r2: ring write slot (2k)
        li   r4, 0                     ; r4: words in this transaction
        rxn  l0, 16, c                 ; one running and two queued per lane
        rxn  l1, 16, c
        rxn  l0, 16, c
        rxn  l1, 16, c
        rxn  l0, 16, c
        rxn  l1, 16, c
        call mark                      ; armed: the host waits for this
m_w:    ld   r1, [r3 + 6]              ; the marker deadline
        li   r0, 1                     ; lane 0 has a word
        wany r1, r0
        cmpi r1, 0
        beq  m_idle                    ; nothing until the deadline: marker
        now  r1
        ld   r0, [r3 + 6]
        sub  r1, r1, r0
        bpl  m_busy                    ; busy past the deadline: marker too
m_word: rx   r0, l0                    ; MOSI; C: CS rose inside this word
        rxn  l0, 16, c                 ; refill first: bookkeeping below may
        rxn  l1, 16, c                 ; take longer than one word
        bcs  m_cut
        rx   r5, l1                    ; MISO of the same edges, popped at
        bcs  m_early                   ; once so both FIFOs drain alike
m_st0:  cmpi r4, 0
        beq  m_first
m_cnt:  addi r4, 1
        ld   r1, [r3 + 1]
        cmp  r1, r4                    ; C set: keep >= count
        bcc  m_drop
        ld   r1, [r3 + 2]              ; free slots = (read - write) mod 512
        sub  r1, r1, r2
        shli r1, r1, 7
        shri r1, r1, 7
        cmpi r1, 2
        beq  m_full                    ; one pair always stays free
        add  r1, r6, r2
        sti  r0, r1
        addi r1, 1
        sti  r5, r1
        addi r2, 2
        shli r2, r2, 7
        shri r2, r2, 7
        st   r2, [r3 + 0]              ; publish the pair
m_next: bra  m_w
m_early:
        lc   r1, EARLY
        sti  r1, r1
        bra  m_st0
m_drop: bra  m_next                    ; beyond P_KEEP: count only
m_full: ld   r0, [r3 + 5]
        addi r0, 1
        st   r0, [r3 + 5]
        bra  m_next
m_idle: call mark
        bra  m_w
m_busy: call mark
        bra  m_word
m_first:
        st   r0, [r3 + 7]
        lc   r1, TMP
        sti  r5, r1
        call ev_fall
        lc   r5, TMP
        ldi  r5, [r5 + 0]
        ld   r0, [r3 + 7]
        bra  m_cnt

; CS rose: r0 = the cut-off MOSI word (0 to 15 bits)
m_cut:  st   r0, [r3 + 7]
        call d1                        ; r1: lane 1's flags; its word to M1W
        stat r0, l0                    ; bit count [15:11]; this clears lane
        mov  r5, r0                    ; 0's sticky flags, so forward them
        shri r5, r5, 11
        andi r5, 31                    ; r5: bits
        shri r0, r0, 1                 ; STAT[8:6] -> [7:5]
        andi r0, 0xE0
        or   r1, r1, r0
        or   r1, r1, r5                ; r1: {e, s, bits}
        cmpi r5, 0
        bne  c_bits
        cmpi r4, 0
        bne  c_ev1                     ; ended on a word boundary
        cmpi r1, 0
        bne  c_ev1                     ; no data, but flags to report
        bra  c_done                    ; CS toggled with no SCK: no record
c_bits: cmpi r4, 0
        bne  c_st
        lc   r5, TMP                   ; a transaction shorter than one word
        sti  r1, r5
        call ev_fall
        lc   r1, TMP
        ldi  r1, [r1 + 0]
c_st:   ld   r5, [r3 + 2]
        sub  r5, r5, r2
        shli r5, r5, 7
        shri r5, r5, 7
        cmpi r5, 2
        beq  c_full
        add  r5, r6, r2
        ld   r0, [r3 + 7]
        sti  r0, r5
        lc   r0, M1W
        ldi  r0, [r0 + 0]
        addi r5, 1
        sti  r0, r5
        addi r2, 2
        shli r2, r2, 7
        shri r2, r2, 7
        st   r2, [r3 + 0]
        bra  c_cnt
c_full: ld   r0, [r3 + 5]
        addi r0, 1
        st   r0, [r3 + 5]
c_cnt:  addi r4, 1
        bra  c_ev1
c_ev1:  mov  r0, r1
        lih  r0, 0xA0                  ; 0xA000 | e << 5 | bits
        mov  r1, r4
        call ev2
c_done: li   r4, 0
        bra  m_next

; CS rose: take lane 1 to its END word without blocking. Both lanes see CS
; rise in the same cycle, so in step lane 1's END is at its FIFO head. If
; it came early (in a pair), or lane 1 has words lane 0 has not, the lanes
; disagree on the SCK edge count: s is set and lane 1 is resynchronised.
; Returns r1 = lane 1 STAT[8:6] in [7:5] | s << 4; M1W = its END word (0 if
; none). Clobbers r0, r5.
d1:     stat r1, l1
        shri r1, r1, 1
        andi r1, 0xE0
        lc   r5, EARLY
        ldi  r0, [r5 + 0]
        cmpi r0, 0
        bne  d1_sk
d1_lp:  now  r0
        li   r5, 2                     ; lane 1 RX FIFO
        wany r0, r5                    ; deadline now: does not block
        cmpi r0, 0
        beq  d1_sk
        rx   r0, l1
        bcs  d1_end
        ori  r1, 0x10                  ; a word lane 0 did not have
        bra  d1_lp
d1_sk:  ori  r1, 0x10
        li   r0, 0
d1_end: lc   r5, EARLY
        sti  r3, r5
        addi r5, 1
        sti  r0, r5                    ; M1W
        ret
; time marker (tail-calls ev2, which returns to mark's caller)
mark:   now  r1
        lc   r0, MARK
        add  r0, r1, r0
        st   r0, [r3 + 6]
        li   r0, 0
        lih  r0, 0x90
        j    ev2

; CS-fall event stamped by lane 2's CAP (lane 2 never runs a command, so
; CAP is the latest CS fall); a timed record, so it moves the deadline
ev_fall:
        lr   r1, l2, cap
        lc   r0, MARK
        add  r0, r1, r0
        st   r0, [r3 + 6]
        li   r0, 0
        lih  r0, 0x80
        j    ev2

; append event (r0, r1) with the current ring slot r2. Clobbers r0, r1,
; r5 and r6 (restored). A full event ring drops the event (counted) and
; sets bit 6 of the write index, which marks the next stored event.
ev2:    ld   r5, [r3 + 3]              ; write index
        ld   r6, [r3 + 4]              ; read index
        sub  r6, r5, r6
        andi r6, 63
        cmpi r6, 60
        beq  e_full
        cmpi r5, 64
        bcc  e_ok
        andi r5, 63                    ; events were dropped before this one
        li   r6, 0
        lih  r6, 1
        or   r0, r0, r6
e_ok:   lc   r6, EVBUF
        add  r6, r6, r5
        sti  r0, r6
        addi r6, 1
        sti  r1, r6
        addi r6, 1
        sti  r2, r6
        addi r5, 4
        andi r5, 63
        st   r5, [r3 + 3]              ; publish
        bra  e_ret
e_full: ori  r5, 64
        st   r5, [r3 + 3]
        ld   r5, [r3 + 5]
        addi r5, 1
        st   r5, [r3 + 5]
e_ret:  lc   r6, P_RING
        ldi  r6, [r6 + 0]
        ret

; ---------------------------------------------------------------- T2
; Sends the pairs up to the next event's slot, then the event; losses as
; they are counted.
pump:   wev  0
        lc   r0, P_RING
        ldi  r6, [r0 + 0]
        li   r3, 0
        li   r2, 0                     ; r2: ring read slot
        li   r5, 0                     ; r5: losses reported so far
p_loop: ld   r1, [r3 + 5]
        cmp  r1, r5
        beq  p_ev
        sub  r0, r1, r5
        mov  r5, r1
        andi r0, 0xFF
        lih  r0, 0xC0
        hput r0
p_ev:   ld   r1, [r3 + 4]              ; event read index
        ld   r0, [r3 + 3]
        andi r0, 63                    ; bit 6 is T0's drop mark
        cmp  r0, r1
        beq  p_noev
        lc   r0, EVBUF
        add  r0, r0, r1
        ldi  r4, [r0 + 2]              ; the event's ring slot
        cmp  r4, r2
        bne  p_data                    ; pairs before it go first
        ldi  r4, [r0 + 0]
        hput r4
        ldi  r4, [r0 + 1]
        hput r4
        addi r1, 4
        andi r1, 63
        st   r1, [r3 + 4]
        bra  p_loop
p_noev: ld   r4, [r3 + 0]              ; up to T0's write slot
p_data: sub  r1, r4, r2
        shli r1, r1, 7
        shri r1, r1, 8                 ; pairs available (Z: none)
        beq  p_loop
        cmpi r1, 15
        bcc  p_n
        li   r1, 15
p_n:    mov  r4, r1
        lih  r4, 0xD0
        hput r4
p_pair: add  r4, r6, r2
        ldi  r0, [r4 + 0]
        hput r0
        ldi  r0, [r4 + 1]
        hput r0
        addi r2, 2
        shli r2, r2, 7
        shri r2, r2, 7
        st   r2, [r3 + 2]              ; free the pair
        djnz r1, p_pair
        bra  p_loop

P_MOSI: .word (UIO0 + 3) << 12 | (UIO0 + 2) << 8 | (UIO0 + 0) << 4 | NOPIN
P_MISO: .word (UIO0 + 3) << 12 | (UIO0 + 2) << 8 | (UIO0 + 1) << 4 | NOPIN
P_CS:   .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 3) << 4 | NOPIN
P_EDGE: .word 1 << 12
P_RING: .word 0x200
P_KEEP: .word 0xFFFF
