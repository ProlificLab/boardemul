#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Which ROM function pointers the flash driver replaces, and with what.

The WiFi driver in flash installs its own implementations over the ROM's during
initialisation: a long run of `l32r <function>; l32r <global>; s32i.n` in one
function. Every one of those globals is named in the linker script, so the run
is a table of `name -> flash address` -- which is the only way this project has
to put a name to a flash routine, and the receive path past `ppEnqueueRxq` is
all flash.

Nothing is inferred: a row is printed only when both literals resolve and the
store is the two-byte `s32i.n <value>, <base>, 0` that follows them.

    python3 rom_overrides.py FLASH_BIN 0x42000000 LINKER_SCRIPT
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def l32r_target(pc: int, blob: bytes, at: int) -> int:
    imm = blob[at + 1] | (blob[at + 2] << 8)
    return ((pc + 3) & ~3) + (imm - 0x10000) * 4


def main() -> None:
    blob = Path(sys.argv[1]).read_bytes()
    base = int(sys.argv[2], 16)
    names: dict[int, str] = {}
    if len(sys.argv) > 3:
        for line in Path(sys.argv[3]).read_text(errors="replace").splitlines():
            found = re.match(r"^/?\*?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                             r"(0x[0-9a-fA-F]+)\s*;", line)
            if found:
                names.setdefault(int(found.group(2), 16), found.group(1))

    def word(at: int) -> int | None:
        off = at - base
        if 0 <= off < len(blob) - 4:
            return int.from_bytes(blob[off:off + 4], "little")
        return None

    rows = []
    for at in range(len(blob) - 8):
        # l32r (3) ; l32r (3) ; s32i.n (2)
        if blob[at] & 0x0F != 0x01 or blob[at + 3] & 0x0F != 0x01:
            continue
        if blob[at + 6] & 0x0F != 0x09 or (blob[at + 7] >> 4) != 0:
            continue
        value_reg, base_reg = blob[at] >> 4, blob[at + 3] >> 4
        if (blob[at + 6] >> 4) != value_reg or (blob[at + 7] & 0x0F) != base_reg:
            continue
        function = word(l32r_target(base + at, blob, at))
        target = word(l32r_target(base + at + 3, blob, at + 3))
        if function is None or target is None:
            continue
        if not (0x3F000000 <= function < 0x43000000):
            continue
        rows.append((base + at, target, function))

    print("%-12s %-34s %s" % ("at", "global", "replaced with"))
    for at, target, function in rows:
        print("0x%08x   %-34s 0x%08x"
              % (at, names.get(target, "0x%08x" % target), function))
    print("%d override(s)" % len(rows))


if __name__ == "__main__":
    main()
