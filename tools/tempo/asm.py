"""Two-pass assembler for Tempo.

Syntax, one statement per line, `;` starts a comment:

    label:  op  operands
            .org   expr          set the location counter
            .equ   NAME, expr    define a constant
            .word  expr, ...     raw 16-bit words
            .entry T, label      record the start address of thread T

Operands: registers r0-r7, lanes l0-l3, lane register names (per, cfg, ...),
branch conditions (eq, ne, ...), and expressions over numbers, labels and
constants with + - * / % << >> & | ^ ~ and parentheses.

Pseudo-instructions:
    lc rd, expr          load any 16-bit constant (1 or 2 words)
    ret                  jr r7
    beq/bne/bcs/bcc/bmi/bpl/bhi/bra label
"""

import ast
import operator
import re
import sys
from dataclasses import dataclass, field

from . import isa

BRANCH_ALIASES = {"b" + c: c for c in isa.COND if c != "al"} | {"bra": "al"}
WAIT_CONDS = {"idle": 0, "pat": 1, "pate": 2, "rx": 3}
LANE_IMM_OPS = ("rxn", "wait")


class AsmError(Exception):
    pass


@dataclass
class Program:
    words: list = field(default_factory=lambda: [0] * isa.IMEM_WORDS)
    used: set = field(default_factory=set)
    symbols: dict = field(default_factory=dict)
    entries: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)  # address -> (line number, source)

    def image(self) -> list:
        return list(self.words)


_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv, ast.Div: operator.floordiv, ast.Mod: operator.mod,
    ast.LShift: operator.lshift, ast.RShift: operator.rshift,
    ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor,
}
_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}


def eval_expr(text: str, symbols: dict) -> int:
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise AsmError(f"bad expression '{text}'") from e

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.Name):
            if n.id not in symbols:
                raise AsmError(f"undefined symbol '{n.id}'")
            return symbols[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            return _BINOPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNOPS:
            return _UNOPS[type(n.op)](ev(n.operand))
        raise AsmError(f"unsupported expression '{text}'")

    return ev(tree)


def split_operands(s: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def reg(tok: str) -> int:
    m = re.fullmatch(r"r([0-7])", tok.strip().lower())
    if not m:
        raise AsmError(f"expected register, got '{tok}'")
    return int(m.group(1))


def lane(tok: str) -> int:
    m = re.fullmatch(r"l([0-3])", tok.strip().lower())
    if not m:
        raise AsmError(f"expected lane l0-l3, got '{tok}'")
    return int(m.group(1))


def size_of(op: str, ops: list, symbols: dict) -> int:
    if op == "lc":
        try:
            v = eval_expr(ops[1], symbols) & 0xFFFF
        except AsmError:
            return 2
        return 1 if isa.sext(v, 16) in range(-256, 256) else 2
    return 1


def expand_includes(src: str, base: str, depth=0) -> str:
    import os
    out = []
    for raw in src.splitlines():
        m = re.match(r'\s*\.include\s+"([^"]+)"', raw)
        if m:
            if depth > 8:
                raise AsmError("include nesting too deep")
            path = os.path.join(base, m.group(1))
            with open(path) as f:
                out.append(expand_includes(f.read(), os.path.dirname(path), depth + 1))
        else:
            out.append(raw)
    return "\n".join(out)


def assemble(src: str, base: str = ".") -> Program:
    src = expand_includes(src, base)
    prog = Program()
    stmts = []
    symbols = {}
    # Pass 1: addresses and constants.
    pc = 0
    for ln, raw in enumerate(src.splitlines(), 1):
        line = raw.split(";", 1)[0].strip()
        while True:
            m = re.match(r"([A-Za-z_.][\w.]*)\s*:(.*)", line)
            if not m:
                break
            name = m.group(1)
            if name in symbols:
                raise AsmError(f"line {ln}: duplicate label '{name}'")
            symbols[name] = pc
            line = m.group(2).strip()
        if not line:
            continue
        parts = line.split(None, 1)
        op = parts[0].lower()
        ops = split_operands(parts[1]) if len(parts) > 1 else []
        try:
            if op == ".equ":
                symbols[ops[0]] = eval_expr(ops[1], symbols)
                continue
            if op == ".org":
                pc = eval_expr(ops[0], symbols)
                continue
            size = len(ops) if op == ".word" else 0 if op == ".entry" else size_of(op, ops, symbols)
        except (AsmError, IndexError) as e:
            raise AsmError(f"line {ln}: {e}") from None
        stmts.append((ln, raw, pc, op, ops, size))
        pc += size
    prog.symbols = symbols
    # Pass 2: encode.
    for ln, raw, pc, op, ops, size in stmts:
        try:
            words = encode_stmt(op, ops, pc, symbols, prog, size)
        except (AsmError, ValueError, IndexError) as e:
            raise AsmError(f"line {ln}: {e}: {raw.strip()}") from None
        if len(words) != size:
            raise AsmError(f"line {ln}: size changed between passes")
        for k, w in enumerate(words):
            a = pc + k
            if not 0 <= a < isa.IMEM_WORDS:
                raise AsmError(f"line {ln}: address {a} outside IMEM")
            if a in prog.used:
                raise AsmError(f"line {ln}: address {a} written twice")
            prog.words[a] = w & 0xFFFF
            prog.used.add(a)
            prog.lines[a] = (ln, raw.strip())
    return prog


def encode_stmt(op, ops, pc, sym, prog, size=1) -> list:
    E = lambda t: eval_expr(t, sym)  # noqa: E731
    enc = isa.encode
    if op == ".word":
        return [E(o) & 0xFFFF for o in ops]
    if op == ".entry":
        prog.entries[E(ops[0])] = E(ops[1])
        return []
    if op == "lc":
        rd, v = reg(ops[0]), E(ops[1]) & 0xFFFF
        if len(ops) != 2:
            raise AsmError("lc takes rd, value")
        s = isa.sext(v, 16)
        if size == 1:
            return [enc("li", rd=rd, imm=s)]
        return [enc("li", rd=rd, imm=v & 0xFF), enc("lih", rd=rd, imm=v >> 8)]
    if op == "ret":
        return [enc("jr", rd=7)]
    if op in BRANCH_ALIASES:
        return [enc("b", rt=isa.COND.index(BRANCH_ALIASES[op]), imm=E(ops[0]) - pc - 1)]
    if op in isa.ALU3_FN:
        return [enc(op, rd=reg(ops[0]), rs=reg(ops[1]), rt=reg(ops[2]))]
    if op in isa.ALU2_FN:
        return [enc(op, rd=reg(ops[0]), rs=reg(ops[1]))]
    if op in isa.ALU2_SHIFT.values():
        return [enc(op, rd=reg(ops[0]), rs=reg(ops[1]), imm=E(ops[2]))]
    if op in ("li", "addi", "cmpi", "andi", "lih", "ori", "xori"):
        return [enc(op, rd=reg(ops[0]), imm=E(ops[1]))]
    if op in ("ld", "st", "ldi"):
        m = re.fullmatch(r"\[\s*(r[0-7])\s*(?:\+\s*(.+))?\]", ops[1].strip())
        if not m:
            raise AsmError("expected [rN + offset]")
        return [enc(op, rd=reg(ops[0]), rs=reg(m.group(1)), imm=E(m.group(2) or "0"))]
    if op == "b":
        if len(ops) == 1:
            ops = ["al"] + ops
        c = ops[0].lower()
        if c not in isa.COND:
            raise AsmError(f"unknown condition '{c}'")
        return [enc("b", rt=isa.COND.index(c), imm=E(ops[1]) - pc - 1)]
    if op == "djnz":
        return [enc("djnz", rd=reg(ops[0]), imm=E(ops[1]) - pc - 1)]
    if op in ("j", "call"):
        return [enc(op, imm=E(ops[0]))]
    if op in isa.SYS_FN:
        if op in ("nop", "halt"):
            return [enc(op)]
        if op in ("sev", "wev"):
            return [enc(op, rs=E(ops[0]))]
        if op in ("iow", "ioe", "wany"):
            return [enc(op, rd=reg(ops[0]), rs=reg(ops[1]))]
        return [enc(op, rd=reg(ops[0]))]
    if op in ("lw", "lr"):
        if op == "lw":
            L, rname, r = lane(ops[0]), ops[1], reg(ops[2])
        else:
            r, L, rname = reg(ops[0]), lane(ops[1]), ops[2]
        rname = rname.strip().lower()
        idx = isa.LANE_REG.index(rname) if rname in isa.LANE_REG else E(rname)
        return [enc(op, lane=L, rd=r, imm=idx)]
    if op in ("tx", "txcrc"):
        L = lane(ops[0])
        if op == "tx":
            rs, n, rest = reg(ops[1]), E(ops[2]), ops[3:]
        else:
            rs, n, rest = 0, E(ops[1]), ops[2:]
        fl = 0
        for f in (rest[0].lower() if rest else ""):
            if f not in "xc":
                raise AsmError(f"unknown tx flag '{f}'")
            fl |= isa.TX_FLAGS[f]
        return [enc(op, lane=L, rs=rs, imm=n, rt=fl)]
    if op in ("rx", "stat"):
        return [enc(op, rd=reg(ops[0]), lane=lane(ops[1]))]
    if op in ("emit", "start", "sync"):
        return [enc(op, lane=lane(ops[0]), rd=reg(ops[1]))]
    if op == "rxn":
        return [enc(op, lane=lane(ops[0]), imm=E(ops[1]) - 1)]
    if op == "wait":
        c = ops[1].strip().lower()
        return [enc(op, lane=lane(ops[0]), imm=WAIT_CONDS[c] if c in WAIT_CONDS else E(c))]
    if op == "stop":
        return [enc(op, lane=lane(ops[0]))]
    raise AsmError(f"unknown instruction '{op}'")


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Tempo assembler")
    p.add_argument("src")
    p.add_argument("-o", "--out", help="hex output, one word per line")
    p.add_argument("-l", "--listing", action="store_true")
    a = p.parse_args(argv)
    import os
    prog = assemble(open(a.src).read(), os.path.dirname(a.src) or ".")
    if a.out:
        with open(a.out, "w") as f:
            for w in prog.words:
                f.write(f"{w:04x}\n")
    if a.listing:
        for addr in sorted(prog.used):
            ln, text = prog.lines[addr]
            print(f"{addr:03x}  {prog.words[addr]:04x}  {isa.disasm(prog.words[addr]):28s} ; {ln}: {text}")


if __name__ == "__main__":
    sys.exit(main())
