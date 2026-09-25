from tempo import isa


def test_every_word_roundtrips():
    """Every legal word re-encodes to itself, so decode and encode agree."""
    legal = 0
    for w in range(1 << 16):
        i = isa.decode(w)
        if i.name == "ill":
            continue
        legal += 1
        assert isa.encode(i.name, rd=i.rd, rs=i.rs, rt=i.rt & (3 if i.name == "txcrc" else 7),
                          imm=i.imm, lane=i.lane) == w, (hex(w), i)
    assert legal == 56832


def test_examples():
    assert isa.disasm(isa.encode("add", rd=1, rs=2, rt=3)) == "add r1, r2, r3"
    assert isa.disasm(isa.encode("b", rt=2, imm=-3)) == "b ne, -3"
    assert isa.disasm(isa.encode("tx", lane=2, rs=4, imm=9, rt=1)) == "tx l2, r4, 9, x"
    assert isa.disasm(isa.encode("rxn", lane=1, imm=9)) == "rxn l1, 10"


def test_verilog_header_is_current():
    import pathlib
    vh = pathlib.Path(__file__).resolve().parents[2] / "src" / "tempo_isa.vh"
    assert vh.read_text() == isa.verilog_header(), "regenerate src/tempo_isa.vh"
