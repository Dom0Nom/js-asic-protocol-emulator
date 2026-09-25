from tempo import asm
from tempo.harness import Harness

SRC = """
        .entry 1, main
main:   li   r1, 0x55
        hput r1
        lc   r2, 0x1234
        hput r2
        now  r3
        halt
tbl:    .word 0xCAFE
"""


def test_boot_through_host_port_and_mailbox():
    h = Harness(SRC, boot="host")
    assert h.boot_done()
    h.run(200)
    t = h.host.mbox_read(2)
    st = h.host.status()
    rr = h.host.read_reg(1, 3)
    ri = h.host.read_imem(h.prog.symbols["tbl"], 1)
    assert h.boot_done()
    assert t.words(2) == [0x55, 0x1234]
    assert st.rx[2] == 0  # thread 1 halted again, no illegal instructions
    assert rr.rx[4:6] == bytes([h.chip.threads[1].regs[3] >> 8, h.chip.threads[1].regs[3] & 0xFF])
    assert ri.words(4) == [0xCAFE]
    assert h.chip.threads[1].retired == 7
