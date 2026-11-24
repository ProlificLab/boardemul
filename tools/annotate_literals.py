#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Disassemble an Xtensa object file with each l32r's literal shown.

Espressif's WiFi libraries (libpp.a and friends) keep their function names,
and each function's literal pool -- the register addresses and masks it
loads -- sits in its own section. objdump shows `l32r a8, 4 <fn-0x10>` and
a relocation; this prints the word that load reads, so a function reads as
the registers it touches (ADR 0186).

    python tools/annotate_literals.py hal_tsf.o [function ...]

OBJDUMP names the Xtensa objdump (PlatformIO's toolchain-xtensa-esp32s3 by
default).
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
from pathlib import Path

DEFAULT_OBJDUMP = (Path.home() / ".platformio" / "packages" / "toolchain-xtensa-esp32s3"
                   / "bin" / "xtensa-esp32s3-elf-objdump")


def objdump() -> str:
    found = os.environ.get("OBJDUMP")
    if found:
        return found
    for suffix in ("", ".exe"):
        candidate = DEFAULT_OBJDUMP.with_name(DEFAULT_OBJDUMP.name + suffix)
        if candidate.exists():
            return str(candidate)
    raise SystemExit("set OBJDUMP to an Xtensa objdump")


def sections(obj: Path) -> dict[str, bytes]:
    """Every section's bytes, by name."""
    out = subprocess.run([objdump(), "-s", str(obj)], capture_output=True,
                         text=True, check=True).stdout
    found: dict[str, bytearray] = {}
    current = None
    for line in out.splitlines():
        head = re.match(r"Contents of section (\S+):", line)
        if head:
            current = found.setdefault(head[1], bytearray())
            continue
        row = re.match(r" ([0-9a-f]{4,}) ((?:[0-9a-f]{2,8} ?){1,4})", line)
        if row and current is not None:
            at = int(row[1], 16)
            data = bytes.fromhex(row[2].replace(" ", ""))
            if len(current) < at:
                current.extend(b"\0" * (at - len(current)))
            current[at:at + len(data)] = data
    return {name: bytes(data) for name, data in found.items()}


def annotate(obj: Path, wanted: set[str]) -> str:
    data = sections(obj)
    out = subprocess.run([objdump(), "-dr", str(obj)], capture_output=True,
                         text=True, check=True).stdout
    lines: list[str] = []
    section = ""
    function = None
    for line in out.splitlines():
        head = re.match(r"Disassembly of section (\S+):", line)
        if head:
            section = head[1]
            continue
        start = re.match(r"[0-9a-f]+ <([^>+-]+)>:", line)
        if start:
            function = start[1]
            if not wanted or function in wanted:
                lines.append(f"\n{function}  [{section}]")
            continue
        if wanted and function not in wanted:
            continue
        if "R_XTENSA" in line or not line.strip():
            continue
        load = re.search(r"l32r\s+(a\d+), ([0-9a-f]+) <", line)
        text = line.rstrip()
        if load and section in data:
            at = int(load[2], 16)
            pool = data[section]
            if at + 4 <= len(pool):
                text += f"    ; {load[1]} = 0x{struct.unpack_from('<I', pool, at)[0]:08x}"
        lines.append(text)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    print(annotate(Path(argv[0]), set(argv[1:])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
