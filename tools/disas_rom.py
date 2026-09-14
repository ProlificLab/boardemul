#!/usr/bin/env python3
"""Disassemble a range of the ROM image offline, enough of Xtensa to read it.

QEMU's monitor disassembles, but only for a running guest, and a run costs
minutes. The ROM is a file in the QEMU tree; the questions asked of it are
about loads, stores and branches, so this decodes those exactly and everything
else by length only. An instruction whose mnemonic is unknown is printed with
its bytes rather than guessed at, which is the difference between this and the
SIMD mnemonics a misaligned disassembler invents.

Length is decidable from the first byte alone: the narrow forms are op0 in
{8, 9, A, B, C, D}, everything else is three bytes.


    python3 disas_rom.py ROM_BIN 0x40041184 60
    python3 disas_rom.py ROM_BIN 0x40041184 60 --symbols logs/rom/symbols.txt
"""
from __future__ import annotations

import bisect
import re
import sys
from pathlib import Path

ROM_BASE = 0x40000000

LSAI = {0: ("l8ui", 1), 1: ("l16ui", 2), 2: ("l32i", 4), 4: ("s8i", 1),
        5: ("s16i", 2), 6: ("s32i", 4), 9: ("l16si", 2), 10: ("movi", 0),
        11: ("l32ai", 4), 12: ("addi", 0), 13: ("addmi", 0)}
# op0 = 7: the compare-and-branch family, indexed by r.
BRI8 = {0: "bnone", 1: "beq", 2: "blt", 3: "bltu", 4: "ball", 5: "bbc",
        6: "bbci", 7: "bbci", 8: "bany", 9: "bne", 10: "bge", 11: "bgeu",
        12: "bnall", 13: "bbs", 14: "bbsi", 15: "bbsi"}
BZ = {0: "beqz", 1: "bnez", 2: "bltz", 3: "bgez"}
BI0 = {0: "beqi", 1: "bnei", 2: "blti", 3: "bgei"}
BI1 = {0: "bf/bt", 1: "?", 2: "bltui", 3: "bgeui"}
# The immediate a B4CONST branch compares against, indexed by its r field.
B4CONST = (-1, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16, 32, 64, 128, 256)
B4CONSTU = (32768, 65536, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16, 32, 64, 128, 256)


def length(first: int) -> int:
    # The density option's narrow forms: L32I.N, S32I.N, ADD.N, ADDI.N and the
    # two ST2/ST3 groups. Getting this set wrong loses alignment two
    # instructions later and the decode turns to noise, which is exactly the
    # failure the docstring warns about.
    return 2 if (first & 0x0F) in (0x8, 0x9, 0xA, 0xB, 0xC, 0xD) else 3


def signed8(value: int) -> int:
    return value - 256 if value >= 128 else value


def decode(pc: int, blob: bytes, at: int) -> tuple[str, int]:
    b0 = blob[at]
    size = length(b0)
    raw = blob[at:at + size]
    op0 = b0 & 0x0F
    t = b0 >> 4

    if size == 3:
        b1, b2 = blob[at + 1], blob[at + 2]
        s, r = b1 & 0x0F, b1 >> 4
        if op0 == 0x1:
            imm = b1 | (b2 << 8)
            target = ((pc + 3) & ~3) + (imm - 0x10000) * 4
            return "l32r a%d, 0x%08x" % (t, target), size
        if op0 == 0x2:
            if r in LSAI:
                name, scale = LSAI[r]
                if name == "movi":
                    # RRI8 lends its s field to a twelve-bit immediate, which
                    # is how the -245 in the receive path is written.
                    value = (s << 8) | b2
                    return "movi a%d, %d" % (t, value - 4096
                                             if value >= 2048 else value), size
                if name in ("addi", "addmi"):
                    return "%s a%d, a%d, %d" % (name, t, s, signed8(b2)
                                                * (256 if name == "addmi" else 1)), size
                return "%s a%d, a%d, %d" % (name, t, s, b2 * scale), size
            return "lsai.%x a%d, a%d, %d  ; %s" % (r, t, s, b2, raw.hex()), size
        if op0 == 0x5:
            imm18 = ((b0 >> 6) | (b1 << 2) | (b2 << 10))
            if imm18 >= 1 << 17:
                imm18 -= 1 << 18
            return "call%d 0x%08x" % ((b0 >> 4) & 3, ((pc & ~3) + 4 + imm18 * 4)), size
        if op0 == 0x6:
            n, m = (b0 >> 4) & 3, (b0 >> 6) & 3
            if n == 0:
                imm18 = (b0 >> 6) | (b1 << 2) | (b2 << 10)
                if imm18 >= 1 << 17:
                    imm18 -= 1 << 18
                return "j 0x%08x" % (pc + 4 + imm18), size
            if n == 1:
                imm12 = b1 >> 4 | (b2 << 4)
                if imm12 >= 1 << 11:
                    imm12 -= 1 << 12
                return "%s a%d, 0x%08x" % (BZ[m], b1 & 0xF, pc + 4 + imm12), size
            if n == 2:
                # The r field is an index into B4CONST, not the constant. It
                # happens to be the constant for indices one to eight, which is
                # why this read correctly for years and then said a branch
                # compared against 13 -- a value B4CONST cannot express. The
                # two it was really comparing against were 64 and 128: a probe
                # request and a beacon (ADR 0062).
                return "%s a%d, %d, 0x%08x" % (BI0[m], b1 & 0xF,
                                               B4CONST[b1 >> 4],
                                               pc + 4 + signed8(b2)), size
            if m == 0:
                return "entry a%d, %d" % (b1 & 0xF, ((b1 >> 4) | (b2 << 4)) * 8), size
            return "%s a%d, %d, 0x%08x" % (BI1[m], b1 & 0xF,
                                           (B4CONSTU if m >= 2 else B4CONST)[b1 >> 4],
                                           pc + 4 + signed8(b2)), size
        if op0 == 0x7:
            name = BRI8[r]
            if name == "bbci" or name == "bbsi":
                bit = (b0 >> 4) | ((r & 1) << 4)
                return "%s a%d, %d, 0x%08x" % (name, s, bit,
                                               pc + 4 + signed8(b2)), size
            return "%s a%d, a%d, 0x%08x" % (name, s, t, pc + 4 + signed8(b2)), size
        if op0 == 0x0:
            op1, op2 = b2 & 0x0F, b2 >> 4
            if op1 == 0 and op2 == 0 and r == 0:
                return "callx/ret  ; %s" % raw.hex(), size
            if op1 == 0 and op2 == 8:
                return "add a%d, a%d, a%d" % (r, s, t), size
            if op1 == 0 and op2 == 0xC:
                return "sub a%d, a%d, a%d" % (r, s, t), size
            if op1 in (4, 5):
                shift = s | ((op1 & 1) << 4)
                return "extui a%d, a%d, %d, %d" % (r, t, shift, op2 + 1), size
            return "qrst.%x.%x a%d, a%d, a%d  ; %s" % (op1, op2, r, s, t,
                                                       raw.hex()), size
        return "op%x  ; %s" % (op0, raw.hex()), size

    b1 = blob[at + 1]
    s, hi = b1 & 0x0F, b1 >> 4
    if op0 == 0x8:
        return "l32i.n a%d, a%d, %d" % (t, s, hi * 4), size
    if op0 == 0x9:
        return "s32i.n a%d, a%d, %d" % (t, s, hi * 4), size
    # The narrow three-register forms put the destination in the field at
    # bits 15..12, not where the wide forms put it. Established from a trace:
    # `1b22` was seen to compute a2 = a2 + 1, which only that reading gives.
    if op0 == 0xA:
        return "add.n a%d, a%d, a%d" % (hi, s, t), size
    if op0 == 0xB:
        return "addi.n a%d, a%d, %d" % (hi, s, t if t else -1), size
    if op0 == 0xC:
        # ST2. The t field decides: its top bit selects a branch, the next bit
        # which one, and what is left of it joins r as the immediate. Settled
        # against a step trace, which is the only reason it is right: two
        # earlier readings each explained one instruction and not the other.
        if t & 0x8:
            offset = ((t & 0x3) << 4) | hi
            return "%s a%d, 0x%08x" % ("bnez.n" if t & 0x4 else "beqz.n",
                                       s, pc + 4 + offset), size
        value = ((t & 0x7) << 4) | hi
        return "movi.n a%d, %d" % (s, value - 128 if value >= 0x60 else value), size
    if op0 == 0xD:
        if hi == 0xF:
            return {0: "ret.n", 1: "retw.n", 2: "break.n",
                    3: "nop.n", 6: "ill.n"}.get(t, "st3  ; " + raw.hex()), size
        if hi == 0x0:
            return "mov.n a%d, a%d" % (t, s), size
        return "st3  ; %s" % raw.hex(), size
    return "narrow  ; %s" % raw.hex(), size


def main() -> None:
    plain = [a for a in sys.argv[1:] if not a.startswith("--")]
    rom = Path(plain[0]).read_bytes()
    start = int(plain[1], 16)
    count = int(plain[2]) if len(plain) > 2 else 40
    base = ROM_BASE
    for flag in sys.argv[1:]:
        if flag.startswith("--base"):
            base = int(flag.split("=", 1)[1], 16)

    table = None
    for flag in sys.argv[1:]:
        if flag.startswith("--symbols"):
            rows = []
            for line in Path(flag.split("=", 1)[1]).read_text().splitlines():
                found = re.match(r"(\S+)\s+stub 0x([0-9a-f]+)\s+code 0x([0-9a-f]+)",
                                 line)
                if found:
                    rows.append((int(found.group(3), 16), found.group(1)))
            rows.sort()
            table = ([r[0] for r in rows], [r[1] for r in rows])

    pc = start
    for _ in range(count):
        at = pc - base
        if not (0 <= at < len(rom) - 3):
            break
        text, size = decode(pc, rom, at)
        label = ""
        if table:
            i = bisect.bisect_right(table[0], pc) - 1
            if i >= 0 and pc == table[0][i]:
                label = "  <%s>" % table[1][i]
        print("0x%08x:  %-8s  %s%s"
              % (pc, rom[at:at + size].hex(), text, label))
        pc += size


if __name__ == "__main__":
    main()
