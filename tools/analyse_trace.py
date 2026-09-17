#!/usr/bin/env python3
"""What ran after an EAPOL frame was delivered, in order, with names.

A fixed window of log lines is the wrong unit: most lines are "Linking TBs"
bookkeeping, and the interesting flash code is hundreds of lines past the
delivery. This walks forward from a delivery to the next one and reports the
distinct program counters in the order first seen, naming each from the ROM
symbol table or from the names recovered by tools/app_symbols.py.

    BOARDEMUL_QEMU_DFLAGS=exec     BOARDEMUL_QEMU_ARGS="-dfilter 0x4003ee00+0x700,0x420c0000+0x18000"       python3 emu/run.py ...
    python3 tools/app_symbols.py firmware/X.bin app.dis > names.txt
    python3 tools/analyse_trace.py logs/x.qemu.log names.txt

Filtered execution tracing is the right instrument for a path whose functions
run for every frame: a breakpoint on one of those slows the guest until the
station never associates (see tools/bp_count.py), while a trace filtered to an
address range costs nothing for anything outside it. The device's own log
lines land in the same file, so a delivery and what ran next interleave.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PC = re.compile(r"^Trace \d+: 0x[0-9a-f]+ \[[0-9a-f]+/0*([0-9a-f]{8})/")
DELIVERED = "a data frame of"


def rom_names() -> dict[int, str]:
    out: dict[int, str] = {}
    path = ROOT / "logs" / "rom" / "symbols.txt"
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        found = re.match(r"^(\S+)\s+stub 0x([0-9a-f]+)\s+code 0x([0-9a-f]+)", line)
        if found:
            out[int(found.group(2), 16)] = found.group(1)
            out[int(found.group(3), 16)] = found.group(1)
    return out


def app_names(path: Path | None) -> dict[int, str]:
    out: dict[int, str] = {}
    if not path or not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        found = re.match(r"^(0x[0-9a-f]{8})\s+(.*)$", line.strip())
        if found:
            out[int(found.group(1), 16)] = found.group(2)
    return out


def main() -> int:
    log = Path(sys.argv[1])
    names = rom_names()
    names.update(app_names(Path(sys.argv[2]) if len(sys.argv) > 2 else None))
    # Optional: start the window at the first delivery whose line contains this
    # marker (e.g. "187 byte" for message three) rather than the first delivery
    # of any kind. Lets one path be read against another that took a different
    # branch -- message one succeeds, message three does not.
    start_marker = sys.argv[3] if len(sys.argv) > 3 else DELIVERED

    inside = False
    seen: list[int] = []
    order: dict[int, int] = {}
    lines = 0
    with log.open(errors="replace") as fh:
        for line in fh:
            if not inside and start_marker in line:
                inside = True
                print(line.rstrip(), flush=True)
                continue
            if inside and DELIVERED in line:
                break                    # the next delivery ends the window
            if not inside:
                continue
            lines += 1
            found = PC.match(line)
            if not found:
                continue
            pc = int(found.group(1), 16)
            if pc not in order:
                order[pc] = len(seen)
                seen.append(pc)

    print("")
    print("%d log lines to the next delivery; %d distinct program counters"
          % (lines, len(seen)))
    print("")
    # Name only the first appearance of each function, so the shape shows
    # rather than every basic block.
    last_name = None
    for pc in seen:
        name = names.get(pc, "")
        if not name:
            # attribute to the nearest named address below it
            below = [a for a in names if a <= pc and pc - a < 0x800]
            if below:
                near = max(below)
                name = "%s+%d" % (names[near], pc - near)
        if name and name != last_name:
            print("  0x%08x  %s" % (pc, name))
            last_name = name
        elif not name:
            print("  0x%08x" % pc)
            last_name = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
