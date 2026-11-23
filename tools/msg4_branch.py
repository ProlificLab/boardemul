#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Which way the transmit path branched, counted rather than deduplicated.

tools/analyse_trace.py reports the distinct program counters of a window in the
order first seen. That is the right shape for "what code is involved", and the
wrong instrument for "which way did this one send go": a basic block already
seen earlier in the window is suppressed at its later position, so a branch
can look untaken when it was taken, or the reverse. Reading a single send's
control flow off it produced a wrong answer once already.

This watches the handful of addresses the decision turns on and prints every
occurrence, in order, with a running count. No deduplication.

The decision, disassembled from the ROM (ppTxProtoProc is 0x4003d6ac):

    0x4003d809  beqz a10, 0x4003d867   ; lmacIsIdle returned; 0 -> abandon
    0x4003d80c  ...                    ; idle: falls through to the transmit
    0x4003d819  callx8 a2              ; a2 = lmac_table[4], the arm
    0x4003d81c  mov.n a2, a10          ; the arm returned
    0x4003d867  movi.n a2, 0; retw.n   ; abandon: return 0, no deferral

So 0x4003d867 after 0x4003d809 means the send was abandoned on a busy queue,
and 0x4003d80c after it means the queue was idle and the arm was called.

    python3 tools/msg4_branch.py QEMU_LOG [MARKER]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PC = re.compile(r"^Trace \d+: 0x[0-9a-f]+ \[[0-9a-f]+/0*([0-9a-f]{8})/")
DELIVERED = "a data frame of"

WATCH = {
    0x4003D809: "beqz on lmacIsIdle      (ppTxProtoProc+349)",
    0x4003D80C: "IDLE: fall through      (ppTxProtoProc+352)",
    0x4003D867: "BUSY: abandon, return 0 (ppTxProtoProc+443)",
    0x4003D81C: "the arm returned        (ppTxProtoProc+368)",
    0x4003B170: "lmacIsIdle",
    0x4003B2CC: "lmacTxFrame (ROM)",
}


def main() -> int:
    log = Path(sys.argv[1])
    marker = sys.argv[2] if len(sys.argv) > 2 else None

    whole: dict[int, int] = {pc: 0 for pc in WATCH}
    after: dict[int, int] = {pc: 0 for pc in WATCH}
    started = marker is None
    order: list[int] = []
    deliveries = 0

    with log.open(errors="replace") as fh:
        for line in fh:
            if DELIVERED in line:
                deliveries += 1
            if marker and not started and marker in line:
                started = True
                print("marker: %s" % line.rstrip(), flush=True)
                continue
            found = PC.match(line)
            if not found:
                continue
            pc = int(found.group(1), 16)
            if pc not in whole:
                continue
            whole[pc] += 1
            if started:
                after[pc] += 1
                if len(order) < 400:
                    order.append(pc)

    print("")
    print("data frames delivered: %d" % deliveries)
    print("")
    print("%-42s %10s %10s" % ("", "whole run", "after mark"))
    for pc, name in WATCH.items():
        print("%-42s %10d %10d" % (name, whole[pc], after[pc]))

    print("")
    print("the order after the mark (every occurrence, first %d):" % len(order))
    run = None
    count = 0
    for pc in order + [None]:
        if pc == run:
            count += 1
            continue
        if run is not None:
            print("  0x%08x  x%-4d %s" % (run, count, WATCH[run]))
        run, count = pc, 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
