#!/usr/bin/env python3
"""Resolve every ROM symbol through its stub to the code it runs.

The linker script names an address per function, and on this part that address
is a twelve-byte trampoline -- a literal, an `l32r`, a `jx` -- so a breakpoint
or a trace filter on the named address catches almost nothing. The real code is
one indirection away, and the indirection is decodable from the ROM image
alone: no run, no guest, no QMP.

    python3 rom_symbols.py ROM_BIN LINKER_SCRIPT [NAME_SUBSTRING]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROM_BASE = 0x40000000
STUB = bytes((0x91, 0xFF, 0xFF, 0xA0, 0x09, 0x00))   # l32r a9, -4 ; jx a9


def main() -> None:
    rom = Path(sys.argv[1]).read_bytes()
    script = Path(sys.argv[2]).read_text(errors="replace")
    want = sys.argv[3].lower() if len(sys.argv) > 3 else ""

    named = re.findall(r"^/?\*?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(0x[0-9a-fA-F]+)\s*;",
                       script, re.M)
    rows = []
    for name, where in named:
        if want and want not in name.lower():
            continue
        at = int(where, 16)
        off = at - ROM_BASE
        if not (0 <= off < len(rom) - 12):
            continue
        head = rom[off:off + 6]
        if head != STUB:
            rows.append((name, at, None, "not a stub"))
            continue
        literal = at - 4
        code = int.from_bytes(rom[literal - ROM_BASE:literal - ROM_BASE + 4], "little")
        rows.append((name, at, code, ""))

    for name, at, code, note in sorted(rows, key=lambda r: r[1]):
        print("%-34s stub 0x%08x  code %s %s"
              % (name, at, "0x%08x" % code if code else "        --", note))
    print("%d symbol(s), %d resolved"
          % (len(rows), sum(1 for r in rows if r[2])))


if __name__ == "__main__":
    main()
