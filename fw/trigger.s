; Triggered pulse generator (host image "trigger"). Lane 1 watches the
; trigger input, lane 0 drives the pulse pin. The pulse starts exactly
; P_DELAYHI * 16384 + P_DELAYLO cycles after the trigger edge as the lane
; saw it (after the input synchroniser), and lasts P_WIDTH cycles. Both
; edges are anchored with `start`, so thread timing cannot move them; if the
; thread is too late for an anchor (delay shorter than the trigger's own
; decode time) lane 0 raises LATE, which the host reads in the status.
;
; P_KIND 1: UART byte. Lane 1 receives 8N1 frames (AUTOSTART on the start
;   bit, so CAP is the start bit's falling edge); a framed byte (stop bit 1)
;   equal to P_MATCH under P_MASK triggers, anchored to that byte's CAP.
; P_KIND 2: edge count. Lane 1 is in capture mode; the P_COUNT-th edge
;   (then every P_COUNT-th) triggers, anchored to its timestamp.
; T1 watches the trigger and never waits on lane 0; T2 schedules the
; pulses. Each pulse sends the host one word: [7:0] pulses so far, [15:8]
; triggers dropped because four pulses were already waiting.

        .include "tempo.inc"

        .entry 1, main
        .entry 2, pulser

; ---- T1: watch the trigger input; post trigger times to T2
; scratch[0..3] trigger times, [4] T1 write index, [5] T2 read index
main:   stop l0
        stop l1
        li   r7, 0
        st   r7, [r7 + 4]
        st   r7, [r7 + 5]
        lc   r0, P_KIND
        ldi  r1, [r0 + 3]              ; P_RXPINS
        lw   l1, pins, r1
        ldi  r1, [r0 + 4]              ; P_LEN
        lw   l1, len0, r1
        ldi  r1, [r0 + 5]              ; P_FRAC
        lw   l1, frac, r1
        ldi  r1, [r0 + 6]              ; P_SAMP
        lw   l1, samp, r1
        ldi  r1, [r0 + 7]              ; P_PAT
        lw   l1, pat, r1
        ldi  r1, [r0 + 0]
        cmpi r1, 2
        beq  edges

; ---- UART byte trigger: two receives always queued, never blocked
        lc   r1, CFG_AUTOSTART
        lw   l1, cfg, r1
        rxn  l1, 10
u_loop: rxn  l1, 10
        rx   r4, l1
        lr   r5, l1, cap               ; the start bit of this byte (read at once)
        lc   r1, 1 << 9
        tst  r4, r1                    ; stop bit must be 1 (a framed byte)
        beq  u_loop
        shri r4, r4, 1
        ldi  r1, [r0 + 9]              ; P_MASK
        and  r4, r4, r1
        ldi  r1, [r0 + 8]              ; P_MATCH
        cmp  r4, r1
        bne  u_loop
        call post
        bra  u_loop

; ---- edge-count trigger
edges:  ldi  r1, [r0 + 10]             ; P_CAPMODE: capture, filter
        lw   l1, mode, r1
        ldi  r4, [r0 + 11]             ; P_COUNT
e_loop: rx   r5, l1                    ; edge timestamp
        djnz r4, e_loop
        ldi  r4, [r0 + 11]
        call post
        bra  e_loop

; post trigger time r5 for T2 (a 4-entry queue); clobbers r1, r2, r3
post:   li   r3, 0
        ld   r1, [r3 + 4]
        ld   r2, [r3 + 5]
        sub  r2, r1, r2
        andi r2, 7
        cmpi r2, 4
        beq  po_full                   ; T2 is 4 pulses behind: drop, count it
        andi r1, 3
        add  r2, r3, r1
        st   r5, [r2 + 0]
        ld   r1, [r3 + 4]
        addi r1, 1
        andi r1, 7
        st   r1, [r3 + 4]
        sev  0
        ret
po_full:
        ld   r1, [r3 + 6]
        addi r1, 1
        st   r1, [r3 + 6]
        ret

; ---- T2: one pulse per posted trigger time
pulser: stop l0
        lc   r0, P_KIND
        ldi  r1, [r0 + 1]              ; P_OUTPINS
        lw   l0, pins, r1
        li   r1, 1
        lw   l0, len0, r1              ; emit slots of one cycle
        ldi  r1, [r0 + 2]              ; P_IDLE: pulse pin idle level (driven)
        emit l0, r1
        li   r6, 0                     ; triggers so far
        li   r7, 0
        st   r7, [r7 + 6]              ; dropped triggers
p_wait: li   r7, 0
        ld   r1, [r7 + 5]
        ld   r2, [r7 + 4]
        cmp  r1, r2
        bne  p_one
        wev  0                         ; T1 posts and signals
        bra  p_wait
p_one:  andi r1, 3
        add  r2, r7, r1
        ld   r5, [r2 + 0]              ; trigger time
        ld   r1, [r7 + 5]
        addi r1, 1
        andi r1, 7
        st   r1, [r7 + 5]
        ldi  r2, [r0 + 12]             ; P_DELAYHI: 16384-cycle steps
        mov  r3, r5
p_far:  cmpi r2, 0
        beq  p_near
        lc   r1, 16384
        add  r3, r3, r1
        waitt r3                       ; coarse wait; the anchor stays exact
        addi r2, -1
        bra  p_far
p_near: ldi  r1, [r0 + 13]             ; P_DELAYLO
        add  r3, r3, r1
        start l0, r3
        ldi  r1, [r0 + 15]             ; P_ACTIVE: pulse level
        emit l0, r1
        ldi  r1, [r0 + 14]             ; P_WIDTH
        add  r3, r3, r1
        start l0, r3
        ldi  r1, [r0 + 2]              ; back to idle
        emit l0, r1
        addi r6, 1
        mov  r1, r6
        ld   r2, [r7 + 6]              ; dropped triggers in the high byte
        swab r2, r2
        or   r1, r1, r2
        hput r1
        bra  p_wait

P_KIND:     .word 1
P_OUTPINS:  .word NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | (UIO0 + 0)
P_IDLE:     .word E_DEN
P_RXPINS:   .word NOPIN << 12 | NOPIN << 8 | (UIO0 + 1) << 4 | NOPIN
P_LEN:      .word 434
P_FRAC:     .word 0
P_SAMP:     .word 217
P_PAT:      .word (PP_I | P_I) << 8 | PP_I
P_MATCH:    .word 0x42
P_MASK:     .word 0xFF
P_CAPMODE:  .word MODE_CAPTURE
P_COUNT:    .word 1
P_DELAYHI:  .word 0
P_DELAYLO:  .word 5000
P_WIDTH:    .word 50
P_ACTIVE:   .word E_D | E_DEN
