; I2C master on lane 3 (host image "i2c"). SDA is the lane's D and I pin,
; SCL its C pin, both open drain; external pull-ups required. Clock
; stretching is honoured on every SCL release, including START and STOP.
; Pins and timing come from the parameter block, patched by the host.
;
; Host word = cmd << 8 | data. Replies (one word) for:
;   1 START (also repeated START); clears the lane's sticky flags   none
;   2 STOP                                                           none
;   3 write byte                              reply: 0 ACK, 1 NACK
;   4 read byte, then ACK                     reply: byte
;   5 read byte, then NACK                    reply: byte
;   6 probe: START, address byte (data), ACK?, a NACKed read byte if R,
;     STOP                                    reply: 0 ACK, 1 NACK
;   7 bus recovery: up to 9 SCL pulses until SDA reads high, then STOP
;                                             reply: pulses used, 0x10 set if SDA stayed low

        .include "tempo.inc"
        .equ SDA_LO, E_DEN
        .equ SDA_HI, E_D | E_DEN
        .equ SCL_LO, E_CEN
        .equ SCL_HI, E_C | E_CEN

        .entry 3, main
main:   stop l3
        lc   r0, P_PINS
        ldi  r1, [r0 + 0]
        lw   l3, pins, r1
        ldi  r1, [r0 + 1]
        lw   l3, len0, r1
        lw   l3, len1, r1
        ldi  r1, [r0 + 2]
        lw   l3, cr, r1
        ldi  r1, [r0 + 3]
        lw   l3, cf, r1
        ldi  r1, [r0 + 4]
        lw   l3, samp, r1
        ldi  r1, [r0 + 5]
        lw   l3, mode, r1
        lc   r1, SYM_NRZ1
        lw   l3, sym1, r1
        lc   r1, CFG_MSB | CFG_DOD | CFG_COD | CFG_CEN | CFG_STRETCH
        lw   l3, cfg, r1
        li   r1, SDA_HI | SCL_HI       ; bus idle, both released
        emit l3, r1
        li   r2, 0xFF

cmd:    hget r4
        shri r5, r4, 8
        and  r4, r4, r2
        cmpi r5, 1
        beq  c_start
        cmpi r5, 2
        beq  c_stop
        cmpi r5, 3
        beq  c_write
        cmpi r5, 4
        beq  c_rack
        cmpi r5, 5
        beq  c_rnack
        cmpi r5, 6
        beq  c_probe
        cmpi r5, 7
        beq  c_recover
        bra  cmd

c_start:
        stat r1, l3                    ; clear sticky flags for this transaction
        call start
        bra  cmd
c_stop: call stop
        bra  cmd
c_write:
        call wbyte
        hput r5
        bra  cmd
c_rack: li   r4, -2                    ; release 8 data bits, drive ACK low
        call rbyte
        hput r5
        bra  cmd
c_rnack:
        li   r4, -1                    ; release all 9 bits: NACK
        call rbyte
        hput r5
        bra  cmd

c_probe:
        mov  r6, r4
        stat r1, l3
        call start
        mov  r4, r6
        call wbyte                     ; r5 = ACK bit
        mov  r3, r5
        bne  p_stop                    ; NACK: straight to STOP
        andi r6, 1
        beq  p_stop                    ; write probe: no data
        li   r4, -1                    ; read probe: the target drives a byte; NACK it
        call rbyte
p_stop: call stop
        hput r3
        bra  cmd

c_recover:
        ldi  r6, [r0 + 6]              ; P_SDAMASK
        li   r3, 0                     ; pulses so far
rc_chk: wait l3, idle
        ior  r1
        and  r1, r1, r6
        bne  rc_ok                     ; SDA high: released
        cmpi r3, 9
        beq  rc_fail
        li   r1, SDA_HI | SCL_LO
        emit l3, r1
        li   r1, SDA_HI | SCL_HI       ; waits out stretching
        emit l3, r1
        addi r3, 1
        bra  rc_chk
rc_fail:
        ori  r3, 0x10
rc_ok:  call stop
        hput r3
        bra  cmd

; START or repeated START; clobbers r1
start:  li   r1, SDA_HI | SCL_LO       ; release SDA while SCL is low
        emit l3, r1
        li   r1, SDA_HI | SCL_HI       ; release SCL, waits out stretching
        emit l3, r1
        li   r1, SDA_LO | SCL_HI       ; SDA falls while SCL high
        emit l3, r1
        li   r1, SDA_LO | SCL_LO
        emit l3, r1
        ret

; STOP; clobbers r1
stop:   li   r1, SDA_LO | SCL_LO
        emit l3, r1
        li   r1, SDA_LO | SCL_HI
        emit l3, r1
        li   r1, SDA_HI | SCL_HI       ; SDA rises while SCL high
        emit l3, r1
        ret

; write byte r4, r5 = ACK bit (0 ACK), flags from r5
wbyte:  shli r4, r4, 1
        ori  r4, 1                     ; release SDA in the ACK slot
        tx   l3, r4, 9, x
        rx   r5, l3
        andi r5, 1
        ret

; read byte with r4 = -2 (ACK) or -1 (NACK), r5 = byte
rbyte:  tx   l3, r4, 9, x
        rx   r5, l3
        shri r5, r5, 1
        and  r5, r5, r2
        ret

; parameter block, patched by the host (host/tempo/i2c.py)
P_PINS:    .word NOPIN << 12 | (UIO0 + 5) << 8 | (UIO0 + 4) << 4 | (UIO0 + 4)
P_SLOT:    .word 125
P_CR:      .word 55
P_CF:      .word 110
P_SAMP:    .word 86
P_MODE:    .word 0
P_SDAMASK: .word 1 << 4
