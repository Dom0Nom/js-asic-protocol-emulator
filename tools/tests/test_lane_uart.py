from tempo.harness import Harness
from tempo.models.uart import UartRx, UartTx

TX_SRC = """
        .include "tempo.inc"
        .equ PER, {per}
        .equ FRACV, {frac}
        .entry 0, main
main:   lc   r1, (NOPIN << 12) | (NOPIN << 8) | (NOPIN << 4) | UO0   ; D = uo[0]
        lw   l0, pins, r1
        lc   r1, PER
        lw   l0, len0, r1
        lw   l0, len1, r1
        lc   r1, FRACV
        lw   l0, frac, r1
        lc   r1, SYM_NRZ1
        lw   l0, sym1, r1
        li   r1, E_D | E_DEN
        emit l0, r1                ; idle high for one slot
        lc   r3, 0x0200
loop:   hget r4
        shli r4, r4, 1
        or   r4, r4, r3
        tx   l0, r4, 10
        bra  loop
"""


def uart_tx_run(baud, data, clock_hz=50e6):
    per = clock_hz / baud
    h = Harness(TX_SRC.format(per=int(per), frac=int(round((per - int(per)) * 256)) & 0xFF),
                clock_hz=clock_hz)
    h.run(100 + per * 2)  # the line is low from reset until firmware emits idle
    rx = h.attach(UartRx("uo0", baud, clock_hz=clock_hz))
    h.chip.h2t.extend(data)
    h.run(per * 10 * (len(data) + 2))
    return h, rx


def test_uart_tx_115200():
    h, rx = uart_tx_run(115200, [0x55, 0x00, 0xFF, 0xA5])
    assert rx.bytes == [0x55, 0x00, 0xFF, 0xA5]
    assert not rx.errors
    assert not h.chip.illegal


def test_uart_tx_fractional_baud_has_no_drift():
    # 50 MHz / 3 Mbaud = 16.67 cycles per bit; fraction keeps the average exact.
    h, rx = uart_tx_run(3_000_000, list(range(20)))
    assert rx.bytes == list(range(20))
    assert not rx.errors
    for p in rx.bit_periods:
        if p is not None:
            assert abs(p - 50e6 / 3e6) < 0.6

RX_SRC = """
        .include "tempo.inc"
        .equ PER, {per}
        .entry 1, main
main:   lc   r1, (NOPIN << 12) | (NOPIN << 8) | ((UI0 + 0) << 4) | NOPIN   ; I = ui[0]
        lw   l1, pins, r1
        lc   r1, PER
        lw   l1, len0, r1
        lc   r1, PER / 2
        lw   l1, samp, r1
        lc   r1, ((PP_I | P_I) << 8) | PP_I      ; start: I was 1, is 0
        lw   l1, pat, r1
        lc   r1, CFG_AUTOSTART
        lw   l1, cfg, r1
        rxn  l1, 10                 ; keep two receives armed
loop:   rxn  l1, 10
        rx   r4, l1
        shri r4, r4, 1
        andi r4, 0xFF
        hput r4
        bra  loop
"""


def uart_rx_run(baud, data, baud_error=0.0, clock_hz=50e6):
    per = round(clock_hz / baud)
    h = Harness(RX_SRC.format(per=per), clock_hz=clock_hz, pullups={"ui0"})
    tx = h.attach(UartTx("ui0", baud, baud_error=baud_error, clock_hz=clock_hz))
    h.run(200)
    tx.send(bytes(data))
    got = []
    for _ in range(int(per * 10 * (len(data) + 2) / 64)):
        h.run(64)
        while h.chip.t2h:
            got.append(h.chip.t2h.popleft())
    return h, got


def test_uart_rx_back_to_back():
    data = [0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80, 0x7E]
    h, got = uart_rx_run(115200, data)
    assert got == data
    assert h.chip.lanes[1].stat == 1 << 3  # only the start-match sticky bit


def test_uart_rx_tolerates_fast_and_slow_senders():
    data = list(range(0, 256, 17))
    for err in (-0.02, 0.02):
        h, got = uart_rx_run(1_000_000, data, baud_error=err)
        assert got == data, err
