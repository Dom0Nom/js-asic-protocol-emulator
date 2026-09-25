; SPI master on lane 2, driven by host mailbox words.
; Needs SPI_MODE (0-3) and SPI_PER (cycles per bit, even, >= 8).
; Pins: MOSI uio0 (D), MISO uio1 (I), SCK uio2 (C), CS uio3 (A).
; Host word: [7:0] byte to send, [15] end the transaction after this byte.
; Every byte exchanged returns the received byte to the host.

        .include "tempo.inc"
        .equ CPOL, SPI_MODE >> 1
        .equ CPHA, SPI_MODE & 1
        .equ HALF, SPI_PER / 2
        ; clock pulse [CR, CF): leading edge at mid-bit for CPHA 0, at bit start for CPHA 1
        .equ SPI_CR, HALF * (1 - CPHA)
        .equ SPI_CF, SPI_CR + HALF
        ; the sampling edge is at HALF in every mode; sample a quarter bit later
        .equ SPI_SAMP, HALF + HALF / 2
        .equ C_IDLE, CPOL * E_C
        .equ CS_LOW, E_DEN | E_AEN | E_CEN | C_IDLE
        .equ CS_HIGH, CS_LOW | E_A

        .entry 2, spi_main
spi_main:
        lc   r1, (UIO0 + 3) << 12 | (UIO0 + 2) << 8 | (UIO0 + 1) << 4 | UIO0
        lw   l2, pins, r1
        lc   r1, SPI_PER
        lw   l2, len0, r1
        lw   l2, len1, r1
        lc   r1, SYM_NRZ1
        lw   l2, sym1, r1
        lc   r1, SPI_CR
        lw   l2, cr, r1
        lc   r1, SPI_CF
        lw   l2, cf, r1
        lc   r1, SPI_SAMP
        lw   l2, samp, r1
        lc   r1, CFG_MSB | CFG_CEN | CPOL * CFG_CPOL
        lw   l2, cfg, r1
        lc   r6, CS_HIGH
        lc   r7, CS_LOW
        emit l2, r6             ; idle: CS high, clock at CPOL
idle:   hget r4                 ; first byte of a transaction
        emit l2, r7             ; CS low for one bit time before the first edge
xfer:   tx   l2, r4, 8, x
        rx   r5, l2
        hput r5
        cmpi r4, 0
        bmi  done               ; bit 15 set: end of transaction
        hget r4
        bra  xfer
done:   emit l2, r6
        bra  idle
