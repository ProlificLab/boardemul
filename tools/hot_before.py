#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The busiest program counters just before something happened.

After the four-way handshake completes the station transmits nothing for about
970 ms and then disassociates (ADR 0074). Nothing-then-leave is the shape of a
driver waiting on a register that never answers -- the same shape as the four
radio bring-up waits in ADR 0019, each of which was found by looking at what
the guest was spinning on.

A spin shows up as a count, not as a sequence: a handful of program counters
executed thousands of times while everything else runs once. So this censuses
the trace lines before a marker and reports the heaviest, which is the
opposite question from `tools/msg4_branch.py` (did this branch go left) and
from `tools/trace_diff.py` (was this code reached at all).

The marker is a line the device logs itself, so it interleaves with the trace.
The disassociation arm -- "a 0xa0 to" -- is the natural one: it is the end of
the window, and the window is everything before it.

    python3 tools/hot_before.py QEMU_LOG "a 0xa0 to" [LINES] [NAMES_FILE]
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PC = re.compile(r"^Trace \d+: 0x[0-9a-f]+ \[[0-9a-f]+/0*([0-9a-f]{8})/")


def names() -> dict[int, str]:
    out: dict[int, str] = {}
    path = ROOT / "logs" / "rom" / "symbols.txt"
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            f = re.match(r"^(\S+)\s+stub 0x([0-9a-f]+)\s+code 0x([0-9a-f]+)", line)
            if f:
                out[int(f.group(2), 16)] = f.group(1)
                out[int(f.group(3), 16)] = f.group(1)
    path = ROOT / "logs" / "names.txt"
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            f = re.match(r"^(0x[0-9a-f]{8})\s+(.*)$", line.strip())
            if f:
                out[int(f.group(1), 16)] = f.group(2)
    return out


def label(pc: int, table: dict[int, str]) -> str:
    if pc in table:
        return table[pc]
    below = [a for a in table if a <= pc and pc - a < 0x800]
    return "%s+%d" % (table[max(below)], pc - max(below)) if below else ""


def main() -> int:
    log = Path(sys.argv[1])
    marker = sys.argv[2]
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 200000
    table = names()

    # Keep a rolling window of the last `width` lines, so the marker can be
    # found in one pass without holding the whole log.
    window: collections.deque[str] = collections.deque(maxlen=width)
    windows: list[list[str]] = []
    with log.open(errors="replace") as fh:
        for line in fh:
            if marker in line:
                windows.append(list(window))
                window.clear()
                continue
            window.append(line)

    print("found %d window(s) ending at %r" % (len(windows), marker))
    if not windows:
        return 0
    # Every window, because which association attempt completed its handshake
    # is recorded in the peer's log and not in this one -- so the spin has to
    # be recognised by its shape, not picked out by position.
    for which, lines in list(enumerate(windows))[-5:]:
        counts: collections.Counter[int] = collections.Counter()
        for line in lines:
            f = PC.match(line)
            if f:
                counts[int(f.group(1), 16)] += 1
        total = sum(counts.values())
        print("")
        print("window %d: %d lines, %d traced blocks, %d distinct"
              % (which, len(lines), total, len(counts)))
        print("")
        print("  %-10s %8s %6s  %s" % ("pc", "count", "share", "name"))
        for pc, n in counts.most_common(12):
            print("  0x%08x %8d %5.1f%%  %s"
                  % (pc, n, 100.0 * n / max(total, 1), label(pc, table)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
