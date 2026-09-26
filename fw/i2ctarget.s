; I2C target emulating a 24C02 EEPROM (host image "i2ctarget"). The 256
; bytes live in the chip's SRAM (one per word at P_MEM); the host can read
; or preload them with IMEM reads and writes while the target runs.
;   lane 0: D = I = SDA (open drain), C = SCL (open drain, driven only to
;           stretch). External-clock receive (MODE.XCLK rising) takes the
;           bits the controller sends; emits drive ACK and data bits.
;           Start pattern START (a receive waits for it), stop pattern START
;           (a repeated START cuts a word short).
;   lane 1: SCL only: start pattern SCL falling (the moment to change SDA).
;   lane 2: start pattern STOP.
; Protocol per Microchip AT24C02C (docs/specs.md 2): device address
; P_ADDR, a word address byte, page writes that roll over inside 8-byte
; pages, sequential reads that roll over at 256. No write-cycle busy time.
; P_STRETCH > 0 holds SCL low for that many cycles after every ACK the
; target gives, to exercise a controller's clock-stretching support.
; Each completed write sends the host one word: the address of the first
; byte written | bytes << 8.

        .include "tempo.inc"
        .equ SCL_RISE, 1 << 12
        .equ START_PAT, (PP_C | PP_I | P_C | P_I) << 8 | (PP_C | PP_I | P_C)
        .equ STOP_PAT,  (PP_C | PP_I | P_C | P_I) << 8 | (PP_C | P_C | P_I)
        .equ SCL_FALL,  (PP_C | P_C) << 8 | PP_C
        .equ SDA_LOW,  E_DEN                 ; drive SDA low, SCL released
        .equ SDA_REL,  E_D | E_DEN           ; SDA released (open drain)
        .equ HOLD_SCL, E_DEN | E_CEN          ; SDA low (ACK) and SCL held low

        .entry 0, main
main:   stop l0
        stop l1
        stop l2
        lc   r0, P_PINS
        ldi  r1, [r0 + 0]
        lw   l0, pins, r1
        ldi  r1, [r0 + 1]              ; P_SCLPIN: lane 1 watches SCL
        lw   l1, pins, r1
        lw   l2, pins, r1              ; lane 2: SDA and SCL, from P_PINS2
        ldi  r1, [r0 + 2]
        lw   l2, pins, r1
        lc   r1, SCL_RISE
        lw   l0, mode, r1
        lc   r1, START_PAT
        lw   l0, pat, r1
        lw   l0, pate, r1
        lc   r1, SCL_FALL
        lw   l1, pat, r1
        lc   r1, STOP_PAT
        lw   l2, pat, r1
        li   r1, 1
        lw   l0, len0, r1              ; emit slots of one cycle
        lc   r1, CFG_MSB | CFG_DOD | CFG_COD | CFG_AUTOSTART
        lw   l0, cfg, r1
        li   r1, SDA_REL
        emit l0, r1
        li   r5, 0                     ; r5: address pointer

; Timing rule: at 400 kHz SDA must change within about 25 cycles of an SCL
; fall, so every pin change is prepared first and issued by the instruction
; right after `wait l1, pat`, and bookkeeping (storing a byte, fetching the
; next) runs while the controller clocks the following byte. Stale SCL-fall
; events from inside a byte are dropped as soon as the byte arrives.

; ---- wait for START and the device address
idle:   stop l0                        ; drop a cut-off word, release the pins
        li   r1, SDA_REL
        emit l0, r1
        rxn  l0, 8                     ; waits for START (AUTOSTART)
        stat r1, l2                    ; forget STOPs from before
        bra  a_wait
a_rep:  rxn  l0, 8, c                  ; after a repeated START: at once
a_wait: wait l0, rx                    ; the address byte is in
        stat r1, l1                    ; (SCL falls inside it are stale)
        rx   r4, l0
        bcs  idle                      ; cut short
        shri r1, r4, 1
        ldi  r2, [r0 + 3]              ; P_ADDR
        cmp  r1, r2
        bne  idle                      ; not us: stay off the bus
        call ack
        andi r4, 1
        bne  rd_first                  ; R: send data

; ---- controller writes: first the word address, then data bytes
        call w_rel
        li   r6, 0                     ; bytes written in this transaction
        call rcv
        bcs  w_end
        call ack
        call w_rel
        andi r4, 0xFF
        mov  r5, r4
w_data: call rcv
        bcs  w_end
        call ack
        call w_rel
        lc   r1, P_MEM
        ldi  r1, [r1 + 0]
        add  r1, r1, r5
        andi r4, 0xFF
        sti  r4, r1
        cmpi r6, 0
        bne  w_next
        mov  r3, r5                    ; first byte's address, for the report
w_next: addi r6, 1
        mov  r1, r5                    ; roll over inside the 8-byte page
        andi r1, -8
        addi r5, 1
        andi r5, 7
        or   r5, r5, r1
        bra  w_data
w_end:  mov  r2, r1                    ; 1 STOP, 0 repeated START
        cmpi r6, 0
        beq  w_how
        shli r6, r6, 8
        or   r3, r3, r6
        hput r3
w_how:  cmpi r2, 0
        beq  a_rep                     ; e.g. a random read: address follows
        bra  idle

; ---- controller reads: sequential from the pointer, rolling over at 256
rd_first:
        rx   r1, l0                    ; the address ACK clock
rd_byte:
        lc   r1, P_MEM
        ldi  r1, [r1 + 0]
        add  r1, r1, r5
        ldi  r4, [r1 + 0]
        addi r5, 1
        andi r5, 0xFF
        li   r6, 8
rd_bit: shri r1, r4, 7                 ; bit 7 first
        andi r1, 1
        ori  r1, E_DEN                 ; SDA = bit (1 releases), SCL released
        wait l1, pat                   ; SCL fell: SDA may change
        emit l0, r1
        rxn  l0, 1, c                  ; take the clock edge (echo unused)
        rx   r1, l0                    ; SCL rose
        shli r4, r4, 1
        djnz r6, rd_bit
        li   r1, SDA_REL               ; the controller's ACK slot
        wait l1, pat
        emit l0, r1
        rxn  l0, 1, c
        rx   r1, l0
        bcs  idle
        andi r1, 1
        bne  idle                      ; NACK: the read is over
        bra  rd_byte                   ; next bit 7 goes out at the next fall

; take one byte from the controller (the receive is already queued):
; r4 = byte, C clear. C set if the byte was cut short: r1 = 0 by a
; repeated START, r1 = 1 by a STOP (lane 2)
rcv:
r_wait: now  r1
        addi r1, 200
        li   r2, 1 | 1 << 6            ; lane 0 word, or lane 2 saw STOP
        wany r1, r2
        mov  r2, r1
        andi r2, 1
        bne  r_got
        andi r1, 1 << 6
        beq  r_wait
        stat r1, l2                    ; STOP: end the transaction
        stop l0
        li   r1, 3
        shri r1, r1, 1                 ; r1 = 1, C = 1
        ret
r_got:  stat r2, l1                    ; SCL falls inside the byte are stale
        rx   r4, l0                    ; C from the lane: cut by a repeated START
        li   r1, 0                     ; (li keeps the flags)
        ret

; ACK the byte just received: at the SCL fall after bit 8 pull SDA low
; (optionally holding SCL low for P_STRETCH cycles first) and take the ACK
; clock edge. The caller releases SDA or drives the next bit at the fall
; after the ACK clock. Every SCL rise the target does not receive on still
; gets a 1-bit receive, since an external-clock edge with no receive queued
; raises UNDER. D is not released with DREL: an emitted level holds until
; the next emit, and on the open-drain SDA an emitted 1 is the release.
ack:    ldi  r2, [r0 + 4]              ; P_STRETCH
        cmpi r2, 0
        bne  a_str
        li   r1, SDA_LOW
        wait l1, pat                   ; SCL fell after bit 8
        emit l0, r1
        rxn  l0, 1, c                  ; take the ACK clock edge
        ret
a_str:  li   r1, HOLD_SCL
        wait l1, pat
        emit l0, r1
        now  r1
        add  r1, r1, r2
        waitt r1
        li   r1, SDA_LOW               ; releases SCL
        emit l0, r1
        rxn  l0, 1, c
        ret

; after an ACK in a write: release SDA at the fall after the ACK clock and
; queue the next byte's receive
w_rel:  li   r1, SDA_REL
        wait l1, pat
        emit l0, r1
        rxn  l0, 8, c
        rx   r1, l0                    ; the ACK clock
        ret

P_PINS:    .word NOPIN << 12 | (UIO0 + 5) << 8 | (UIO0 + 4) << 4 | (UIO0 + 4)
P_SCLPIN:  .word NOPIN << 12 | (UIO0 + 5) << 8 | NOPIN << 4 | NOPIN
P_PINS2:   .word NOPIN << 12 | (UIO0 + 5) << 8 | (UIO0 + 4) << 4 | NOPIN
P_ADDR:    .word 0x50
P_STRETCH: .word 0
P_MEM:     .word 0x300
