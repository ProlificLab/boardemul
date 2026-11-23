#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Wrap a firmware image's segments in an ELF, so a real disassembler will read it.

`tools/disas_rom.py` decodes enough Xtensa to read a function's shape, and that
was enough for years of register-level work. It is not enough for reading a
path through the vendor's WiFi stack: about a third of the instructions in that
region come back as raw hex, sync is lost after every one it cannot size, and a
misaligned listing once sent an investigation after a function that does not
exist (ADR 0068).

The toolchain's own objdump decodes all of it, but it **segfaults** on a raw
binary -- `-b binary -m xtensa` crashes in binutils, because the Xtensa
disassembler wants the ISA configuration an ELF carries and a flat file has
none. So this writes the smallest ELF that satisfies it: one section per
loadable segment, at the address the image says it loads at, marked executable.

No new dependency is added by this file. It is stdlib Python, and the objdump
it feeds is whichever one the reader already has -- on the machine this was
written on, the one PlatformIO installs for this board:

    .platformio/packages/toolchain-xtensa-esp32s3/bin/xtensa-esp32s3-elf-objdump

Nothing in the build or the test suite uses either, which is what keeps this
inside ADR 0001: it is a tool for reading, not a part of the emulator.

    python3 tools/image_to_elf.py FIRMWARE.bin OUT.elf [--offset 0x10000]
    xtensa-esp32s3-elf-objdump -d OUT.elf

Segment addresses come from the image's own header via `mkflash.parse_image`
(ADR 0003: parse, do not assume), so a firmware whose layout differs from the
one this was written against still lands at the right addresses.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mkflash import parse_image  # noqa: E402

EM_XTENSA = 94
ET_EXEC = 2
SHT_PROGBITS = 1
SHT_STRTAB = 3
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4
SHF_WRITE = 0x1

EHDR = 52
SHDR = 40


def build(segments: list[tuple[int, bytes, str]]) -> bytes:
    """One ELF, one section per segment, each at its load address.

    Sections rather than program headers because that is what objdump's
    disassembly walks: it needs sh_addr to label instructions and
    SHF_EXECINSTR to disassemble them rather than dump them as data.
    """
    names = b"\0" + b"".join(n.encode() + b"\0" for _, _, n in segments) + b".shstrtab\0"
    count = len(segments) + 2                       # null, segments, shstrtab

    body = b""
    offsets = []
    at = EHDR + SHDR * count
    for _, data, _ in segments:
        pad = (-at) % 4
        body += b"\0" * pad
        at += pad
        offsets.append(at)
        body += data
        at += len(data)
    names_at = at
    body += names

    out = bytearray()
    out += b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
    out += struct.pack("<HHIIIIIHHHHHH",
                       ET_EXEC, EM_XTENSA, 1,
                       segments[0][0] if segments else 0,   # entry
                       0,                                   # no program headers
                       EHDR,                                # section headers
                       0x300,                               # flags, as the ABI uses
                       EHDR, 0, 0, SHDR, count, count - 1)

    out += bytes(SHDR)                                       # the null section
    name_at = 1
    for i, (addr, data, name) in enumerate(segments):
        # Only the code segments are marked executable. Marking the data ones
        # too makes objdump disassemble constants and string tables as
        # instructions, and half a million lines of that drowns every search
        # through the listing -- the first version of this file did exactly
        # that, and a grep for one bit test came back with thirty-four hits in
        # the string table.
        code = ".irom" in name or ".iram" in name or ".rtc" in name
        flags = SHF_ALLOC | (SHF_EXECINSTR if code else SHF_WRITE)
        out += struct.pack("<IIIIIIIIII", name_at, SHT_PROGBITS, flags,
                           addr, offsets[i], len(data), 0, 0, 4, 0)
        name_at += len(name) + 1
    out += struct.pack("<IIIIIIIIII", name_at, SHT_STRTAB, 0, 0,
                       names_at, len(names), 0, 0, 1, 0)
    out += body
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", type=Path, help="a flash image, or a bare app image")
    ap.add_argument("out", type=Path)
    ap.add_argument("--offset", default="0x10000",
                    help="where the app image starts in a flash image "
                         "(default 0x10000, the usual factory partition)")
    args = ap.parse_args()

    data = args.image.read_bytes()
    offset = int(args.offset, 0)
    image = parse_image(data, offset)

    segments = []
    for i, seg in enumerate(image.segments):
        # Name each one for its region, so a listing says where it is running
        # from rather than just which number it was.
        # file_offset is relative to the start of the app image, not to the
        # start of the file: the first segment reports 0x20, which is the
        # twenty-four byte image header plus one eight byte segment header.
        # Reading it as absolute put every section 0x10000 bytes early and
        # produced a listing that decoded cleanly and meant nothing.
        at = offset + seg.file_offset
        blob = data[at:at + seg.length]
        segments.append((seg.load_addr, blob, ".seg%d.%s"
                         % (i, seg.region.split()[0].lower())))
    if not segments:
        print("no segments in that image", file=sys.stderr)
        return 1

    args.out.write_bytes(build(segments))
    print("%s: %d segment(s)" % (args.out, len(segments)))
    for addr, blob, name in segments:
        print("  0x%08x  %8d bytes  %s" % (addr, len(blob), name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
