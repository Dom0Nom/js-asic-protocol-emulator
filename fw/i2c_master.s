; I2C master on lane 3, driven by host mailbox commands.
; Needs I2C_SLOT (cycles per bit). SDA uio4 (D and I), SCL uio5 (C), both
; open drain with external pull-ups. Clock stretching is honoured on every
; SCL release, including START, repeated START and STOP.
;
; Host word = cmd << 8 | data, reply for commands that return something:
;   1 START (also repeated START)     no reply
;   2 STOP                            no reply
;   3 write data byte                 reply: 0 ACK, 1 NACK
;   4 read byte, then ACK             reply: byte
;   5 read byte, then NACK            reply: byte

        .include "tempo.inc"
        .equ I2C_CR, I2C_SLOT * 44 / 100      ; SCL rises (released)
        .equ I2C_CF, I2C_SLOT * 88 / 100      ; SCL falls
        .equ I2C_SAMP, (I2C_CR + I2C_CF) / 2 + 4  ; middle of the high phase, after the stretch check
        .equ SDA_LO, E_DEN
        .equ SDA_HI, E_D | E_DEN
        .equ SCL_LO, E_CEN
        .equ SCL_HI, E_C | E_CEN

        .entry 3, i2c_main
i2c_main:
        lc   r1, NOPIN << 12 | (UIO0 + 5) << 8 | (UIO0 + 4) << 4 | (UIO0 + 4)
        lw   l3, pins, r1
        lc   r1, I2C_SLOT
        lw   l3, len0, r1
        lw   l3, len1, r1
        lc   r1, SYM_NRZ1
        lw   l3, sym1, r1
        lc   r1, I2C_CR
        lw   l3, cr, r1
        lc   r1, I2C_CF
        lw   l3, cf, r1
        lc   r1, I2C_SAMP
        lw   l3, samp, r1
        lc   r1, CFG_MSB | CFG_DOD | CFG_COD | CFG_CEN | CFG_STRETCH
        lw   l3, cfg, r1
        li   r2, 0xFF

cmd:    hget r4
        shri r5, r4, 8
        and  r4, r4, r2
        cmpi r5, 1
        beq  start
        cmpi r5, 2
        beq  stop
        cmpi r5, 3
        beq  write
        cmpi r5, 4
        beq  read_ack
        cmpi r5, 5
        beq  read_nack
        bra  cmd

start:  li   r1, SDA_HI | SCL_LO     ; release SDA while SCL is low (repeated START setup)
        emit l3, r1
        li   r1, SDA_HI | SCL_HI     ; release SCL, waits out any stretching
        emit l3, r1
        li   r1, SDA_LO | SCL_HI     ; SDA falls while SCL high: START
        emit l3, r1
        li   r1, SDA_LO | SCL_LO
        emit l3, r1
        bra  cmd

stop:   li   r1, SDA_LO | SCL_LO
        emit l3, r1
        li   r1, SDA_LO | SCL_HI
        emit l3, r1
        li   r1, SDA_HI | SCL_HI     ; SDA rises while SCL high: STOP
        emit l3, r1
        bra  cmd

write:  shli r4, r4, 1
        ori  r4, 1                   ; release SDA in the ACK slot
        tx   l3, r4, 9, x
        rx   r5, l3
        andi r5, 1                   ; bit 0 of the echo is the ACK slot
        hput r5
        bra  cmd

read_ack:
        li   r4, -2                  ; 0x...FE: release 8 data bits, drive ACK low
        bra  read
read_nack:
        li   r4, -1                  ; release all 9 bits: NACK
read:   tx   l3, r4, 9, x
        rx   r5, l3
        shri r5, r5, 1
        and  r5, r5, r2
        hput r5
        bra  cmd
