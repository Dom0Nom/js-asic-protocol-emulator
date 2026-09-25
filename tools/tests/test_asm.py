import pytest

from tempo import asm, isa

SRC = """
        .equ BAUD, 434
        .entry 0, start
start:  lc   r1, BAUD
        lc   r2, 0x1234
        lc   r3, far        ; forward reference, always 2 words
loop:   add  r4, r4, r1
        djnz r2, loop
        bne  loop
        ld   r5, [r6 + 3]
        tx   l1, r5, 10, xc
        lw   l0, len0, r1
        lr   r2, l3, cap
        rxn  l2, 16
        wait l2, rx
        ret
far:    .word 0xBEEF, far
"""


def test_program():
    p = asm.assemble(SRC)
    d = [isa.disasm(p.words[a]) for a in sorted(p.used)]
    assert d[:2] == ["li r1, 178", "lih r1, 1"]
    assert p.words[p.symbols["far"]] == 0xBEEF
    assert p.words[p.symbols["far"] + 1] == p.symbols["far"]
    assert "djnz r2, -2" in d and "b ne, -3" in d
    assert "tx l1, r5, 10, xc" in d and "rxn l2, 16" in d and "jr r7" in d and "lr r2, l3, cap" in d
    assert p.entries == {0: p.symbols["start"]}


@pytest.mark.parametrize("bad", ["add r1, r2", "li r1, 300", "b xx, 0", "foo r1", "rxn l4, 1"])
def test_errors(bad):
    with pytest.raises(asm.AsmError):
        asm.assemble(bad)
