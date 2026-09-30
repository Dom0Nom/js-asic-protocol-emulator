; One resident image: UART TX, Manchester TX, a memory workload and
; host-commanded I2C master. Pin assignments are disjoint, and only the
; I2C service uses the mailboxes. Timing constants assume 50 MHz.
; T0/T1 initialize their lanes and halt; host resumes them at the K_ PCs.

        .equ I2C_SLOT, 125
        .include "i2c_master.s"
        .equ MC_REPEAT, 1
        .include "manchester_core.inc"

        .entry 0, demo_uart_init
demo_uart_init:
        lc   r1, NOPIN << 12 | NOPIN << 8 | NOPIN << 4 | UIO0
        lw   l0, pins, r1
        li   r1, 50
        lw   l0, len0, r1
        lw   l0, len1, r1
        lc   r1, SYM_NRZ1
        lw   l0, sym1, r1
        li   r1, E_D | E_DEN
        emit l0, r1
        wait l0, idle
        halt

K_UART_RUN:
demo_uart_run:
        lc   r1, 0x200 | (0x55 << 1)
        lc   r2, 0x200 | (0xAA << 1)
demo_uart_loop:
        tx   l0, r1, 10
        tx   l0, r2, 10
        bra  demo_uart_loop

        .entry 2, demo_memory
demo_memory:
        li   r0, 7
        li   r2, 0
        lc   r1, demo_constant
demo_memory_loop:
        addi r2, 1
        st   r2, [r0]
        ld   r3, [r0]
        ldi  r4, [r1]
        bra  demo_memory_loop
demo_constant:
        .word 0xA596
