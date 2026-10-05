; CAN 2.0A data frame transmitter (11-bit identifier), thread 0, lane 0.
; Needs CAN_CLK_KHZ (core clock in kHz) and CAN_KBPS (bit rate in kbit/s).
; Bus on uio0, open drain with an external pull-up: dominant is driven low,
; recessive is released, and the same pin is read back.
;
; Host words: header id << 4 | dlc (dlc 0-8), then the data bytes two per
; word, first byte in the high half; an odd last byte alone in the low half.
; Reply, one word per frame:
;   0 sent, acknowledged
;   1 sent, no ACK
;   2 arbitration lost: nothing is driven after the lost bit, no retry
;   3 a recessive bit read dominant after arbitration (the last bit of a
;     lane command is not checked), or the lane ran dry; the rest of the
;     frame was still sent and no error frame follows
;
; A frame starts after 11 recessive bit times. SOF, stuffing, CRC-15 and the
; bit grid are the lane's. The thread polls for a lost arbitration every 20
; cycles and stops the lane before the DLC command starts, which leaves it
; the three bits after the identifier.

        .include "tempo.inc"
        .equ BIT256, (CAN_CLK_KHZ * 256 + CAN_KBPS / 2) / CAN_KBPS
        .equ BITLEN, BIT256 >> 8            ; 25 MHz, 125 kbit/s: 200 cycles
        .equ BITFRAC, BIT256 & 255
        .equ BUS, UIO0 + 0
        .equ CAN_CFG, CFG_MSB | CFG_DOD
        .equ STUFF5, CFG_STUFFANY | 5 << CFG_STUFF_SH
        .equ IDLE, E_D | E_EOF              ; one released slot that may end a frame

        .entry 0, can_tx
can_tx: lc   r1, NOPIN << 12 | NOPIN << 8 | BUS << 4 | BUS
        lw   l0, pins, r1
        lc   r1, BITLEN
        lw   l0, len0, r1
        lw   l0, len1, r1
        lc   r1, BITFRAC
        lw   l0, frac, r1
        lc   r1, BITLEN * 3 / 4 + 4         ; 75% of the bit at the pad: 3 cycles in, 1 out
        lw   l0, samp, r1
        lc   r1, SYM_NRZ1
        lw   l0, sym1, r1
        lc   r1, P_I << 8                   ; start flag while the bus is dominant
        lw   l0, pat, r1
        lc   r1, 0x4599 << 1                ; CRC-15 in the top 15 bits of the CRC register
        lw   l0, poly, r1
        lc   r1, MODE_FRAME | MODE_ARB
        lw   l0, mode, r1
        lc   r2, IDLE
        li   r7, -1
frame:  hget r5
        mov  r6, r5
        andi r6, 15                         ; data bytes
        mov  r4, r6
        addi r4, 1
        shri r4, r4, 1
        beq  ready
        li   r3, 0
load:   hget r1
        st   r1, [r3 + 0]
        addi r3, 1
        djnz r4, load
ready:  lc   r1, CAN_CFG | STUFF5
        lw   l0, cfg, r1
        li   r1, 0
        lw   l0, crc, r1
        emit l0, r2                         ; a finished emit clears the stuff run that a stop leaves
        lc   r4, 11 * BITLEN
        stat r1, l0
idle:   now  r1
        add  r1, r1, r4
        waitt r1
        stat r1, l0
        andi r1, ST_START
        bne  idle                           ; dominant within the last 11 bit times
        shri r1, r5, 4
        shli r1, r1, 3
        tx   l0, r1, 15, xc                 ; SOF, identifier, RTR, two reserved bits
        tx   l0, r5, 4, xc                  ; DLC
arb:    stat r1, l0
        shli r3, r1, 6
        bmi  lost                           ; ARBLOST
        andi r1, ST_RXAV
        beq  arb                            ; until the first command has ended
        li   r3, 0
data:   addi r6, -2
        bmi  odd
        ld   r1, [r3 + 0]
        tx   l0, r1, 16, xc
        rx   r1, l0                         ; echo of the command two back
        addi r3, 1
        bra  data
odd:    addi r6, 1
        bne  crc
        ld   r1, [r3 + 0]
        tx   l0, r1, 8, xc
        rx   r1, l0
crc:    txcrc l0, 15, x
        rx   r1, l0
        li   r1, CAN_CFG
        lw   l0, cfg, r1                    ; stuffing ends with the CRC sequence
        tx   l0, r7, 2, x                   ; CRC delimiter, ACK slot
        rx   r1, l0
        tx   l0, r7, 7, x                   ; ACK delimiter, EOF bits 1-6
        rx   r1, l0
        emit l0, r2                         ; EOF bit 7
        rx   r4, l0                         ; bit 0: ACK slot level
        rx   r1, l0
        xori r1, 0x7F
        bne  bad                            ; dominant in the ACK delimiter or EOF
        stat r1, l0
        lc   r3, ST_ARBLOST | ST_UNDER
        tst  r1, r3
        bne  bad
        andi r4, 1
        bra  done
lost:   stop l0
        li   r4, 2
        bra  done
bad:    li   r4, 3
done:   hput r4
        bra  frame
