; SPI master on lane 2 (host image "spi"). MOSI is the lane's D pin, MISO
; I, SCK C, CS A (active low). Mode, bit time and sample point come from the
; parameter block, patched by the host (host/tempo_host/spi.py).
;
; Host words: [15:12] op, [11:0] n (bytes, 1..4095). Data travels two bytes
; per word, first byte in the high half; an odd last byte is in the high half.
;   1 WRITE n   CS low, then n bytes from the host; MISO is ignored
;   2 READ n    CS low, then n bytes clocked out with MOSI low; the bytes go
;               to the host. SCK pauses (legal in SPI) when the host is slow.
;   3 END       CS high
;   4 XFER n    CS low, full duplex: n bytes in, n bytes back
;   5 BULK k    CS low, then k blocks of 512 bytes read into a two-half SRAM
;               buffer at P_BUF (one block = one half = 256 words, first byte
;               in the high half of each word). After each half the thread sends the half
;               number (0 or 1); it reuses a half only after the host has
;               read it and sent any word back. The host reads the buffer
;               with the IMEM read command, 512 bytes per transaction, in
;               fetch slots of the halted threads.

        .include "tempo.inc"

        .entry 2, main
main:   stop l2
        lc   r0, P_PINS
        ldi  r1, [r0 + 0]
        lw   l2, pins, r1
        ldi  r1, [r0 + 1]
        lw   l2, len0, r1
        lw   l2, len1, r1
        ldi  r1, [r0 + 2]
        lw   l2, cr, r1
        ldi  r1, [r0 + 3]
        lw   l2, cf, r1
        ldi  r1, [r0 + 4]
        lw   l2, samp, r1
        ldi  r1, [r0 + 5]
        lw   l2, cfg, r1
        lc   r1, SYM_NRZ1
        lw   l2, sym1, r1
        call cs_high

cmd:    hget r4
        shri r5, r4, 12
        lc   r1, 0x0FFF
        and  r4, r4, r1                ; r4 = n
        cmpi r5, 1
        beq  c_write
        cmpi r5, 2
        beq  c_read
        cmpi r5, 3
        beq  c_end
        cmpi r5, 4
        beq  c_xfer
        cmpi r5, 5
        beq  c_bulk
        bra  cmd

c_end:  call cs_high
        bra  cmd

; ---- WRITE n
c_write:
        call cs_low
w_loop: cmpi r4, 2
        bcc  w_tail                    ; n < 2
        hget r2
        tx   l2, r2, 16
        addi r4, -2
        bra  w_loop
w_tail: cmpi r4, 0
        beq  cmd
        hget r2
        shri r2, r2, 8
        tx   l2, r2, 8
        bra  cmd

; ---- READ n: keep one transfer queued behind the running one
c_read: call cs_low
        li   r2, 0
        cmpi r4, 2
        bcc  r_tail
        tx   l2, r2, 16, x             ; prime
        addi r4, -2
r_loop: cmpi r4, 2
        bcc  r_drain
        tx   l2, r2, 16, x
        addi r4, -2
        rx   r5, l2
        hput r5
        bra  r_loop
r_drain:
        rx   r5, l2
        hput r5
r_tail: cmpi r4, 0
        beq  cmd
        tx   l2, r2, 8, x
        rx   r5, l2
        shli r5, r5, 8
        hput r5
        bra  cmd

; ---- XFER n
c_xfer: call cs_low
x_loop: cmpi r4, 2
        bcc  x_tail
        hget r2
        tx   l2, r2, 16, x
        rx   r5, l2
        hput r5
        addi r4, -2
        bra  x_loop
x_tail: cmpi r4, 0
        beq  cmd
        hget r2
        shri r2, r2, 8
        tx   l2, r2, 8, x
        rx   r5, l2
        shli r5, r5, 8
        hput r5
        bra  cmd

; ---- BULK k: scratch[0] = halves the host has freed (credits)
c_bulk: call cs_low                   ; r4 = halves to fill
        li   r3, 0                     ; current half
        li   r2, 0
        li   r1, 2
        st   r1, [r2 + 0]              ; both halves free
b_half: li   r2, 0
        ld   r1, [r2 + 0]
        cmpi r1, 0
        bne  b_fill
        hget r5                        ; wait for the host to free a half
        li   r1, 1
b_fill: addi r1, -1
        st   r1, [r2 + 0]
        ldi  r1, [r0 + 8]              ; P_BUF
        cmpi r3, 0
        beq  b_base
        lc   r2, 256
        add  r1, r1, r2
b_base: lc   r2, 256
        li   r5, 0
b_word: tx   l2, r5, 16, x
        rx   r5, l2
        sti  r5, r1
        li   r5, 0
        addi r1, 1
        djnz r2, b_word
        hput r3                        ; half r3 is ready
        xori r3, 1
        djnz r4, b_half
        li   r2, 0                     ; take back the host's frees
        ld   r1, [r2 + 0]
b_ack:  cmpi r1, 2
        beq  cmd
        hget r5
        addi r1, 1
        bra  b_ack

; CS low if it is not already (r6 = 1 while low); clobbers r1
cs_low: cmpi r6, 1
        beq  cs_ret
        ldi  r1, [r0 + 6]              ; P_CSLOW: CS low, clock idle
        emit l2, r1
        li   r6, 1
cs_ret: ret

cs_high:
        ldi  r1, [r0 + 7]              ; P_CSHIGH
        emit l2, r1
        li   r6, 0
        ret

P_PINS:   .word (UIO0 + 3) << 12 | (UIO0 + 2) << 8 | (UIO0 + 1) << 4 | UIO0
P_PER:    .word 8
P_CR:     .word 4
P_CF:     .word 8
P_SAMP:   .word 7
P_CFG:    .word CFG_MSB | CFG_CEN
P_CSLOW:  .word E_DEN | E_AEN | E_CEN
P_CSHIGH: .word E_DEN | E_AEN | E_CEN | E_A
P_BUF:    .word 0x200
