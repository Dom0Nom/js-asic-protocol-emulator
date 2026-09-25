from tempo import asm
from tempo.sim import Chip


def run(src, cycles=400, threads=(0,)):
    p = asm.assemble(src)
    c = Chip()
    c.load(p.words)
    for t in threads:
        c.start(t, p.entries.get(t, 0))
    for _ in range(cycles):
        c.step(0)
    return c, p


def test_arith_and_loop():
    c, _ = run("""
        li   r1, 10
        li   r2, 0
    lp: add  r2, r2, r1
        djnz r1, lp
        lc   r3, 0x8001
        shri r4, r3, 1      ; C = 1
        halt
    """)
    t = c.threads[0]
    assert t.regs[2] == 55 and t.regs[1] == 0
    assert t.regs[4] == 0x4000 and t.c == 1
    assert not t.run


def test_threads_interleave_every_4_cycles():
    c, _ = run("""
        .entry 0, a
        .entry 1, a
    a:  now r1
        now r2
        halt
    """, threads=(0, 1))
    t0, t1 = c.threads[0].regs, c.threads[1].regs
    assert t0[2] - t0[1] == 4 and t1[2] - t1[1] == 4
    assert t1[1] - t0[1] == 1


def test_mailbox_and_events():
    src = """
        .entry 0, prod
        .entry 1, cons
    prod: li r1, 7
        hput r1
        sev 2
        halt
    cons: wev 2
        li r2, 1
        halt
    """
    c, _ = run(src, threads=(0, 1))
    assert list(c.t2h) == [7]
    assert c.threads[1].regs[2] == 1


def test_waitt_until_time():
    c, _ = run("""
        now  r1
        addi r1, 100
        waitt r1
        now  r2
        halt
    """)
    r = c.threads[0].regs
    assert 4 <= r[2] - r[1] < 8  # waitt releases in [0, 4), then one more slot


def test_ldi_reads_imem_in_two_slots():
    c, p = run("""
        li   r1, 0
        now  r3
        ldi  r2, [r1 + tbl]
        now  r4
        halt
    tbl: .word 0xBEEF
    """)
    r = c.threads[0].regs
    assert r[2] == 0xBEEF
    assert r[4] - r[3] == 12  # ldi takes two slots
