#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""What the firmware runs after one kind of frame but not after another.

Message one is answered: the station builds message two, submits it, and the
peer receives it. Message three is not answered. Both arrive by the same
receive path, in the same run, minutes apart. So the interesting object is not
either window on its own but the *difference* between them: whatever the
answered frame reaches and the unanswered one never does is the path message
four fails to take, and whatever only the unanswered frame reaches is where it
went instead.

This is a set comparison, which is what a deduplicating window gives
correctly. It is deliberately NOT an ordering -- reading order off a first-seen
window is what produced the wrong answer in ADR 0072, and
`tools/msg4_branch.py` is the instrument for that question.

Windows run from a delivery line to the next delivery line, and every window
matching a marker is unioned, so five association attempts reinforce rather
than compete.

    python3 tools/trace_diff.py QEMU_LOG "of 131" "of 187" [NAMES_FILE]
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


def label(pc: int, names: dict[int, str]) -> str:
    if pc in names:
        return names[pc]
    below = [a for a in names if a <= pc and pc - a < 0x800]
    if below:
        near = max(below)
        return "%s+%d" % (names[near], pc - near)
    return ""


def main() -> int:
    log = Path(sys.argv[1])
    mark_a, mark_b = sys.argv[2], sys.argv[3]
    names = rom_names()
    names.update(app_names(Path(sys.argv[4]) if len(sys.argv) > 4 else
                           ROOT / "logs" / "names.txt"))

    # Which window each line belongs to: None, or the marker that opened it.
    seen: dict[str, set[int]] = {mark_a: set(), mark_b: set()}
    counts = {mark_a: 0, mark_b: 0}
    window: str | None = None
    with log.open(errors="replace") as fh:
        for line in fh:
            if DELIVERED in line:
                window = None
                for mark in (mark_a, mark_b):
                    if mark in line:
                        window = mark
                        counts[mark] += 1
                continue
            if window is None:
                continue
            found = PC.match(line)
            if found:
                seen[window].add(int(found.group(1), 16))

    a, b = seen[mark_a], seen[mark_b]
    print("windows: %d after %r, %d after %r" % (counts[mark_a], mark_a,
                                                 counts[mark_b], mark_b))
    print("distinct program counters: %d and %d, %d shared"
          % (len(a), len(b), len(a & b)))

    for title, group in (("ONLY after %r (the answered frame)" % mark_a, a - b),
                         ("ONLY after %r (the unanswered frame)" % mark_b, b - a)):
        print("")
        print("=== %s: %d" % (title, len(group)))
        # Collapse to one line per named function, with how many of its blocks
        # are in the set -- a function with many blocks only here is a better
        # lead than a single stray block.
        by_func: dict[str, list[int]] = {}
        for pc in sorted(group):
            name = label(pc, names)
            key = name.split("+")[0] if name else "0x%08x" % pc
            by_func.setdefault(key, []).append(pc)
        for key in sorted(by_func, key=lambda k: -len(by_func[k])):
            pcs = by_func[key]
            print("  %-44s %3d block(s)  %s" % (
                key, len(pcs),
                " ".join("0x%08x" % p for p in pcs[:4])
                + (" ..." if len(pcs) > 4 else "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
