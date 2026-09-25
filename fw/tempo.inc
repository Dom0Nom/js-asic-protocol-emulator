; Tempo hardware constants. Values follow docs/architecture.md.

; CFG bits
.equ CFG_MSB,       1 << 0
.equ CFG_DOD,       1 << 1
.equ CFG_COD,       1 << 2
.equ CFG_CEN,       1 << 3
.equ CFG_CPOL,      1 << 4
.equ CFG_STRETCH,   1 << 5
.equ CFG_NRZI,      1 << 6
.equ CFG_DIFF,      1 << 7
.equ CFG_STUFF_SH,  8
.equ CFG_STUFFANY,  1 << 11
.equ CFG_AUTOSTART, 1 << 12
.equ CFG_DREL,      1 << 13

; MODE bits
.equ MODE_FRAME,    1 << 3
.equ MODE_CRCINV,   1 << 4
.equ MODE_RS_RISE,  1 << 5
.equ MODE_RS_FALL,  2 << 5
.equ MODE_RS_BOTH,  3 << 5
.equ MODE_CAPTURE,  1 << 7
.equ MODE_ARB,      1 << 8
.equ MODE_FILT_SH,  9

; SYM fields
.equ SYM_B,         1 << 15
.equ SYM_A,         1 << 14
.equ SYM_REL,       1 << 13
.equ SYM_NRZ0,      0
.equ SYM_NRZ1,      SYM_A | SYM_B

; PINS: pins(d, i, c, a) with 15 = none
.equ NOPIN, 15

; emit word bits
.equ E_D,   1 << 0
.equ E_A,   1 << 1
.equ E_C,   1 << 2
.equ E_DEN, 1 << 4
.equ E_AEN, 1 << 5
.equ E_CEN, 1 << 6
.equ E_EOF, 1 << 7                  ; end of frame (FRAME mode)

; STAT bits
.equ ST_BUSY, 1 << 0
.equ ST_QFULL, 1 << 1
.equ ST_RXAV, 1 << 2
.equ ST_START, 1 << 3
.equ ST_STOP, 1 << 4
.equ ST_LATE, 1 << 5
.equ ST_UNDER, 1 << 6
.equ ST_STUFFERR, 1 << 7
.equ ST_OVERRUN, 1 << 8
.equ ST_ARBLOST, 1 << 9

; wait conditions and pattern bit positions {prev A,C,I,D, cur A,C,I,D}
.equ W_IDLE, 0
.equ W_PAT, 1
.equ W_PATE, 2
.equ W_RX, 3
.equ P_D, 1
.equ P_I, 2
.equ P_C, 4
.equ P_A, 8
.equ PP_D, 16
.equ PP_I, 32
.equ PP_C, 64
.equ PP_A, 128

; pin indices
.equ UIO0, 0
.equ UO0, 8
.equ UI0, 8
