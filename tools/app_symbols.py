#!/usr/bin/env python3
"""Name the vendor firmware's own functions, out of the strings it carries.

The image has no symbol table, which is why years of this project's notes
quote bare addresses. But ESP-IDF's asserts and log calls pass `__func__`, so
a function that contains one loads a pointer to its own name out of DROM. That
is enough to recover a few hundred names:

  * walk an objdump listing, tracking the most recent `entry` as the current
    function;
  * for every `l32r` in it, resolve the literal to a value;
  * if that value points at a NUL-terminated C identifier, the function
    mentions that name.

It is a heuristic, and it says so: a function that logs someone else's name
gets credited with it, which is why several addresses below carry two or three.
Treat a single name as good evidence and a list as a hint. It named
`ieee80211_auth` at the address independent work had already found by stepping,
which is the check that made it worth keeping.

    python3 tools/image_to_elf.py firmware/FIRMWARE.bin app.elf
    xtensa-esp32s3-elf-objdump -d app.elf > app.dis
    python3 tools/app_symbols.py firmware/FIRMWARE.bin app.dis

Add `--grep WORD` to keep only names containing a word, which is how the WiFi
receive path was found among five hundred of them.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mkflash import parse_image  # noqa: E402

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{3,63}$")
LINE = re.compile(r"^([0-9a-f]{8}):\s+\S+\s+(\S+)\s*(.*)$")


class Memory:
    """The image's loadable segments, addressable by load address."""

    def __init__(self, data: bytes, offset: int):
        self.spans = []
        for seg in parse_image(data, offset).segments:
            # file_offset is relative to the image, not the file (see
            # image_to_elf.py, where reading it as absolute cost a listing).
            at = offset + seg.file_offset
            self.spans.append((seg.load_addr, seg.load_addr + seg.length,
                               data[at:at + seg.length]))

    def word(self, addr: int) -> int | None:
        for lo, hi, blob in self.spans:
            if lo <= addr < hi - 3:
                return struct.unpack_from("<I", blob, addr - lo)[0]
        return None

    def string(self, addr: int, limit: int = 64) -> str | None:
        for lo, hi, blob in self.spans:
            if lo <= addr < hi:
                end = blob.find(b"\0", addr - lo)
                if end < 0 or end - (addr - lo) > limit:
                    return None
                return blob[addr - lo:end].decode("ascii", "replace")
        return None


def names(memory: Memory, listing: Path) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    current = None
    for line in listing.read_text(errors="replace").splitlines():
        found = LINE.match(line)
        if not found:
            continue
        pc, op, rest = int(found.group(1), 16), found.group(2), found.group(3)
        if op == "entry":
            current = pc
        elif op == "l32r" and current is not None:
            literal = rest.rsplit(",", 1)[-1].strip()
            if not literal.startswith("0x"):
                continue
            value = memory.word(int(literal, 16))
            if value is None:
                continue
            text = memory.string(value)
            if text and IDENTIFIER.match(text):
                out.setdefault(current, set()).add(text)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", type=Path)
    ap.add_argument("listing", type=Path, help="objdump -d output for that image")
    ap.add_argument("--offset", default="0x10000")
    ap.add_argument("--grep", default=None,
                    help="only names containing this, case-insensitively")
    args = ap.parse_args()

    memory = Memory(args.image.read_bytes(), int(args.offset, 0))
    table = names(memory, args.listing)

    shown = 0
    for addr in sorted(table):
        found = sorted(table[addr])
        if args.grep:
            found = [n for n in found if args.grep.lower() in n.lower()]
            if not found:
                continue
        print("0x%08x  %s" % (addr, ", ".join(found)))
        shown += 1
    print("# %d function(s) named%s" % (shown, ", filtered" if args.grep else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
