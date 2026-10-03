; USB low-speed (1.5 Mbit/s) packet PHY.
; Needs USB_CLK_KHZ, the core clock in kHz (25000 at the declared 25 MHz).
; D- on uio6, D+ on uio7. Idle J (D- high) comes from the device-side
; pull-up on D-. Lane 0 transmits, lane 1 receives.
;
; Thread 0, transmit. Host words: header [15] CRC16 follows, [11:8] number
; of data bytes, [7:0] PID byte (with check nibble); then one word per data
; byte. SYNC, NRZI, bit stuffing, CRC16 and EOP are done by the lane.
;
; Thread 1, receive. Buffers a packet in scratch, two bytes per word (SYNC
; and PID included, first byte in the low half), and after EOP pushes to the
; host: the words, then an end word 0xE000 | [8] CRC16 residual ok |
; [7:4] byte count | [3:0] bits in the last partial byte. The host SPI port
; cannot drain bytes at USB speed, so whole packets are handed over.
; Packets longer than 16 bytes (SYNC + PID + 8 data + CRC16 is 12) are cut.

        .include "tempo.inc"
        .equ BIT256, (USB_CLK_KHZ * 256 + 750) / 1500   ; cycles per bit * 256, rounded
        .equ BITLEN, BIT256 >> 8            ; 25 MHz: 16 + 171/256 = 16.668 cycles
        .equ BITFRAC, BIT256 & 255
        .equ DM, UIO0 + 6
        .equ DP, UIO0 + 7
        .equ USB_CFG, CFG_NRZI | (6 << CFG_STUFF_SH)
        .equ J_DRIVE, E_D | E_DEN | E_AEN   ; D- high, D+ low
        .equ SE0, E_DEN | E_AEN
        .equ RESIDUAL, 0xB001               ; CRC16 residual, reflected

        .entry 0, usb_tx
usb_tx:
        lc   r1, DP << 12 | NOPIN << 8 | NOPIN << 4 | DM
        lw   l0, pins, r1
        li   r1, BITLEN
        lw   l0, len0, r1
        lw   l0, len1, r1
        li   r1, BITFRAC
        lw   l0, frac, r1
        lc   r1, SYM_NRZ1
        lw   l0, sym1, r1
        lc   r1, USB_CFG | CFG_DIFF
        lw   l0, cfg, r1
        lc   r1, MODE_CRCINV
        lw   l0, mode, r1
        lc   r1, 0xA001                     ; CRC16 polynomial, reflected
        lw   l0, poly, r1
        lc   r2, J_DRIVE
        li   r3, SE0
        li   r4, 0                          ; release both lines
tx_pkt: hget r5                             ; header
        lc   r1, 0xFFFF
        lw   l0, crc, r1                    ; queued: cannot disturb a packet in flight
        emit l0, r2                         ; J, sets the NRZI line state
        swab r6, r5                         ; r6 = header >> 8 in the low byte
        andi r6, 15                         ; data byte count
        shli r7, r5, 8
        li   r1, 0x80                       ; SYNC byte: seven zeros, then a one
        or   r7, r7, r1
        tx   l0, r7, 16                     ; SYNC and PID, not in the CRC
        cmpi r6, 0
        beq  tx_crc
tx_dat: hget r1
        tx   l0, r1, 8, c
        djnz r6, tx_dat
tx_crc: cmpi r5, 0
        bpl  tx_eop                         ; header bit 15 clear: no CRC16
        txcrc l0, 16
tx_eop: emit l0, r3                         ; SE0
        emit l0, r3                         ; SE0
        emit l0, r2                         ; J
        emit l0, r4                         ; release to the pull-up
        bra  tx_pkt

        .entry 1, usb_rx
usb_rx:
        lc   r1, DP << 12 | NOPIN << 8 | DM << 4 | NOPIN
        lw   l1, pins, r1
        li   r1, BITLEN
        lw   l1, len0, r1
        li   r1, BITFRAC
        lw   l1, frac, r1
        li   r1, BITLEN / 2
        lw   l1, samp, r1
        lc   r1, (PP_I | P_I) << 8 | PP_I   ; SOP: D- falls from idle J
        lw   l1, pat, r1
        lc   r1, (P_I | P_A) << 8           ; SE0: both lines low ends a packet
        lw   l1, pate, r1
        lc   r1, 0xA001
        lw   l1, poly, r1
        lc   r1, MODE_RS_BOTH
        lw   l1, mode, r1
        lc   r1, USB_CFG | CFG_AUTOSTART
        lw   l1, cfg, r1
        lc   r6, 0xFFFF
        lc   r7, RESIDUAL
        rxn  l1, 8                          ; SYNC of the first packet, starts on SOP
        rxn  l1, 8, c                       ; PID, continues the frame
rx_pkt: lw   l1, crc, r6                    ; applied between PID and first data byte
        li   r3, 0                          ; word index
rx_even:
        rxn  l1, 8, c                       ; keep one receive queued ahead
        rx   r2, l1
        bcs  rx_end_e                       ; C: cut short by SE0
        rxn  l1, 8, c
        rx   r4, l1
        bcs  rx_end_o
        swab r4, r4
        or   r4, r4, r2
        st   r4, [r3 + 0]
        addi r3, 1
        bra  rx_even                        ; at most 8 slots between receives: SE0 to SOP can be 3 bit times
rx_end_o:
        stop l1                             ; drop the receive queued ahead
        rxn  l1, 8                          ; re-arm for the next SOP first
        lr   r4, l1, crc                    ; before SYNC bits reach the CRC; the reset is queued at rx_pkt
        rxn  l1, 8, c                       ; PID: queued before SYNC ends, the push below takes longer
        st   r2, [r3 + 0]
        shli r1, r3, 1
        addi r1, 1                          ; byte count
        bra  rx_fin
rx_end_e:
        stop l1
        rxn  l1, 8
        lr   r4, l1, crc
        rxn  l1, 8, c
        shli r1, r3, 1                      ; byte count
rx_fin: stat r3, l1                         ; bit count of the cut-short word in [15:11]; stop keeps it
        shri r3, r3, 11
        shli r2, r1, 4
        or   r3, r3, r2                     ; byte count in [7:4]
        lc   r5, 0xE000
        or   r3, r3, r5
        cmp  r4, r7
        bne  rx_bad
        lc   r5, 0x100                      ; residual ok
        or   r3, r3, r5
rx_bad: addi r1, 1
        shri r1, r1, 1                      ; words to push
        li   r2, 0
rx_push:
        cmpi r1, 0
        beq  rx_done
        ld   r4, [r2 + 0]
        hput r4
        addi r2, 1
        addi r1, -1
        bra  rx_push
rx_done:
        hput r3
        bra  rx_pkt
