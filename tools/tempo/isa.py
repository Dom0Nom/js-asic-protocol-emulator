"""Tempo instruction set: the single source of truth for encodings.

The assembler, disassembler, simulator decoder and the generated Verilog
header (`src/tempo_isa.vh`) all come from the tables in this module.
"""

from dataclasses import dataclass

NREGS = 8
NLANES = 4
NTHREADS = 4
IMEM_WORDS = 1024
SCRATCH_WORDS = 32

# Major opcodes, bits [15:12].
OP_ALU3, OP_ALU2, OP_LI, OP_ADDI, OP_CMPI, OP_ANDI, OP_IMM8, OP_MEM = range(8)
OP_B, OP_DJNZ, OP_J, OP_SYS, OP_LREG, OP_LDI, OP_TX, OP_LANE = range(8, 16)

ALU3_FN = ["add", "sub", "and", "or", "xor", "shl", "shr", "asr"]
ALU2_FN = ["mov", "not", "neg", "cmp", "tst", "par", "brev", "swab"]
ALU2_SHIFT = {1: "shli", 2: "shri", 3: "rori"}  # fn[5:4] selects, fn[3:0] = k
COND = ["al", "eq", "ne", "cs", "cc", "mi", "pl", "hi"]
SYS_FN = ["nop", "halt", "jr", "now", "waitt", "ior", "iow", "ioe",
          "hput", "hget", "sev", "wev", "tid", "hcnt", "wany", "tstat"]
LANE_SUB = ["rx", "rxn", "emit", "start", "sync", "wait", "stop", "stat"]
LANE_REG = ["cfg", "pins", "len0", "len1", "sym0", "sym1", "frac", "cr",
            "cf", "samp", "pat", "pate", "crc", "poly", "cap", "mode"]
TX_FLAGS = {"x": 1, "c": 2, "s": 4}
IMM9_OPS = ["li", "addi", "cmpi", "andi"]


def sext(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


@dataclass(frozen=True)
class Insn:
    """A decoded instruction. Unused fields are 0."""
    op: int
    name: str
    rd: int = 0
    rs: int = 0
    rt: int = 0
    imm: int = 0
    lane: int = 0

    def __str__(self) -> str:
        return disasm_insn(self)


def decode(w: int) -> Insn:
    """Decode one 16-bit word. Reserved encodings decode to name 'ill'."""
    w &= 0xFFFF
    op = w >> 12
    a = (w >> 9) & 7
    b = (w >> 6) & 7
    c = (w >> 3) & 7
    if op == OP_ALU3:
        return Insn(op, ALU3_FN[w & 7], rd=a, rs=b, rt=c)
    if op == OP_ALU2:
        fn = w & 0x3F
        if fn >> 4:
            return Insn(op, ALU2_SHIFT[fn >> 4], rd=a, rs=b, imm=fn & 15)
        if fn < len(ALU2_FN):
            return Insn(op, ALU2_FN[fn], rd=a, rs=b)
        return Insn(op, "ill")
    if op in (OP_LI, OP_ADDI, OP_CMPI, OP_ANDI):
        return Insn(op, IMM9_OPS[op - OP_LI], rd=a, imm=sext(w, 9))
    if op == OP_IMM8:
        if not (w >> 8) & 1:
            return Insn(op, "lih", rd=a, imm=w & 0xFF)
        return Insn(op, "xori" if (w >> 7) & 1 else "ori", rd=a, imm=w & 0x7F)
    if op == OP_MEM:
        return Insn(op, "st" if (w >> 5) & 1 else "ld", rd=a, rs=b, imm=w & 0x1F)
    if op == OP_LDI:
        return Insn(op, "ldi", rd=a, rs=b, imm=w & 0x3F)
    if op == OP_B:
        return Insn(op, "b", rt=a, imm=sext(w, 9))
    if op == OP_DJNZ:
        return Insn(op, "djnz", rd=a, imm=sext(w, 9))
    if op == OP_J:
        if (w >> 10) & 1:
            return Insn(op, "ill")
        return Insn(op, "call" if (w >> 11) & 1 else "j", imm=w & 0x3FF)
    if op == OP_SYS:
        fn = w & 0x3F
        if fn >= len(SYS_FN):
            return Insn(op, "ill")
        return Insn(op, SYS_FN[fn], rd=a, rs=b)
    lane = (w >> 10) & 3
    if op == OP_LREG:
        if w & 3:
            return Insn(op, "ill")
        return Insn(op, "lw" if (w >> 2) & 1 else "lr", lane=lane, rd=c,
                    imm=(w >> 6) & 15)
    if op == OP_TX:
        fl = w & 7
        return Insn(op, "txcrc" if fl & 4 else "tx", lane=lane, rs=c,
                    imm=((w >> 6) & 15) + 1, rt=fl)
    return Insn(op, LANE_SUB[w & 7], lane=lane, rd=c, imm=(w >> 6) & 15)


def encode(name: str, rd=0, rs=0, rt=0, imm=0, lane=0) -> int:
    """Encode from already-resolved numeric fields. Raises ValueError."""

    def chk(v, lo, hi, what):
        if not lo <= v <= hi:
            raise ValueError(f"{name}: {what} {v} out of range [{lo}, {hi}]")
        return v

    for r in (rd, rs, rt):
        chk(r, 0, 7, "register")
    chk(lane, 0, NLANES - 1, "lane")
    if name in ALU3_FN:
        return (OP_ALU3 << 12) | rd << 9 | rs << 6 | rt << 3 | ALU3_FN.index(name)
    if name in ALU2_FN:
        return (OP_ALU2 << 12) | rd << 9 | rs << 6 | ALU2_FN.index(name)
    for sel, n in ALU2_SHIFT.items():
        if name == n:
            return (OP_ALU2 << 12) | rd << 9 | rs << 6 | sel << 4 | chk(imm, 0, 15, "shift")
    if name in IMM9_OPS:
        op = OP_LI + IMM9_OPS.index(name)
        return (op << 12) | rd << 9 | (chk(imm, -256, 255, "imm") & 0x1FF)
    if name == "lih":
        return (OP_IMM8 << 12) | rd << 9 | chk(imm, 0, 255, "imm")
    if name in ("ori", "xori"):
        sel = 3 if name == "xori" else 2
        return (OP_IMM8 << 12) | rd << 9 | sel << 7 | chk(imm, 0, 127, "imm")
    if name in ("ld", "st"):
        return (OP_MEM << 12) | rd << 9 | rs << 6 | (name == "st") << 5 | chk(imm, 0, 31, "offset")
    if name == "ldi":
        return (OP_LDI << 12) | rd << 9 | rs << 6 | chk(imm, 0, 63, "offset")
    if name == "b":
        return (OP_B << 12) | rt << 9 | (chk(imm, -256, 255, "offset") & 0x1FF)
    if name == "djnz":
        return (OP_DJNZ << 12) | rd << 9 | (chk(imm, -256, 255, "offset") & 0x1FF)
    if name in ("j", "call"):
        return (OP_J << 12) | (name == "call") << 11 | chk(imm, 0, 1023, "target")
    if name in SYS_FN:
        return (OP_SYS << 12) | rd << 9 | rs << 6 | SYS_FN.index(name)
    if name in ("lw", "lr"):
        return (OP_LREG << 12) | lane << 10 | chk(imm, 0, 15, "lane register") << 6 | rd << 3 | (name == "lw") << 2
    if name in ("tx", "txcrc"):
        fl = rt | (4 if name == "txcrc" else 0)
        return (OP_TX << 12) | lane << 10 | (chk(imm, 1, 16, "bit count") - 1) << 6 | rs << 3 | fl
    if name in LANE_SUB:
        return (OP_LANE << 12) | lane << 10 | chk(imm, 0, 15, "imm") << 6 | rd << 3 | LANE_SUB.index(name)
    raise ValueError(f"unknown instruction {name}")


def disasm_insn(i: Insn) -> str:
    n = i.name
    if n == "ill":
        return "ill"
    if i.op == OP_ALU3:
        return f"{n} r{i.rd}, r{i.rs}, r{i.rt}"
    if i.op == OP_ALU2:
        if n in ALU2_SHIFT.values():
            return f"{n} r{i.rd}, r{i.rs}, {i.imm}"
        return f"{n} r{i.rd}, r{i.rs}"
    if i.op in (OP_LI, OP_ADDI, OP_CMPI, OP_ANDI, OP_IMM8):
        return f"{n} r{i.rd}, {i.imm}"
    if i.op in (OP_MEM, OP_LDI):
        return f"{n} r{i.rd}, [r{i.rs}+{i.imm}]"
    if i.op == OP_B:
        return f"b {COND[i.rt]}, {i.imm:+d}"
    if i.op == OP_DJNZ:
        return f"djnz r{i.rd}, {i.imm:+d}"
    if i.op == OP_J:
        return f"{n} {i.imm}"
    if i.op == OP_SYS:
        if n in ("nop", "halt"):
            return n
        if n in ("sev", "wev"):
            return f"{n} {i.rs}"
        if n in ("iow", "ioe", "wany"):
            return f"{n} r{i.rd}, r{i.rs}"
        return f"{n} r{i.rd}"
    L = f"l{i.lane}"
    if n == "lw":
        return f"lw {L}, {LANE_REG[i.imm]}, r{i.rd}"
    if n == "lr":
        return f"lr r{i.rd}, {L}, {LANE_REG[i.imm]}"
    if i.op == OP_TX:
        fl = "".join(k for k, v in TX_FLAGS.items() if i.rt & v and k != "s")
        tail = f", {fl}" if fl else ""
        if n == "txcrc":
            return f"txcrc {L}, {i.imm}{tail}"
        return f"tx {L}, r{i.rs}, {i.imm}{tail}"
    if n in ("rx", "stat"):
        return f"{n} r{i.rd}, {L}"
    if n in ("emit", "start", "sync"):
        return f"{n} {L}, r{i.rd}"
    if n == "rxn":
        return f"rxn {L}, {i.imm + 1}"
    if n == "wait":
        return f"wait {L}, {i.imm}"
    return f"{n} {L}"


def disasm(w: int) -> str:
    return disasm_insn(decode(w))


def verilog_header() -> str:
    """Localparams for the RTL decoder. Regenerate with
    `python -m tempo.isa > ../src/tempo_isa.vh` from tools/."""
    out = ["// Generated by tools/tempo/isa.py. Do not edit.", ""]

    def lp(name, val, width=None):
        v = f"{width}'d{val}" if width else str(val)
        out.append(f"localparam {name} = {v};")

    for name, val in sorted(((k, v) for k, v in globals().items() if k.startswith("OP_")), key=lambda kv: kv[1]):
        lp(name, val, 4)
    for i, n in enumerate(ALU3_FN):
        lp(f"ALU3_{n.upper()}", i, 3)
    for i, n in enumerate(ALU2_FN):
        lp(f"ALU2_{n.upper()}", i, 6)
    for sel, n in ALU2_SHIFT.items():
        lp(f"ALU2SEL_{n.upper()}", sel, 2)
    for i, n in enumerate(COND):
        lp(f"COND_{n.upper()}", i, 3)
    for i, n in enumerate(SYS_FN):
        lp(f"SYS_{n.upper()}", i, 6)
    for i, n in enumerate(LANE_SUB):
        lp(f"LANE_{n.upper()}", i, 3)
    for i, n in enumerate(LANE_REG):
        lp(f"LREG_{n.upper()}", i, 4)
    for n, v in TX_FLAGS.items():
        lp(f"TXF_{n.upper()}", v, 3)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    print(verilog_header(), end="")
