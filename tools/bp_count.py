#!/usr/bin/env python3
"""Arm breakpoints on named addresses and count which ones the guest reaches.

The question this answers is "does execution get this far", which for a path
through a firmware with no symbols is most of the work. It is deliberately a
counter and not a tracer: a stop per instruction slows this guest about fifty
times and it never finishes a scan, while a handful of breakpoints on functions
that are rarely hit leaves it running well enough to associate -- measured, a
run with two armed still managed a hundred and thirty-one associations.

**A negative result here is worthless without a control.** "Nothing was hit"
and "the thing we were waiting for never happened" look identical from inside
the debugger, and the first version of this probe could not tell them apart:
it reported zero hits on a run whose logs had been thrown away, which proved
nothing at all. So the caller must check, from the run's own logs, that the
event was in flight while the breakpoints were armed -- and this prints the
window it armed for so that check can be made against it.

    python3 tools/bp_count.py PORT ARM_AFTER WATCH 0x420c2374 0x420c2478 ...

Breakpoints are armed late on purpose: stopping on every probe response
stretches the first scan indefinitely and the station never gets as far as
associating, so anything past the scan needs the guest left alone for the
first minute.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rsp import Rsp  # noqa: E402


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__.strip().splitlines()[-4].strip(), file=sys.stderr)
        return 2
    port = int(sys.argv[1])
    arm_after = float(sys.argv[2])
    watch = float(sys.argv[3])
    wanted = [int(a, 0) for a in sys.argv[4:]]

    link = Rsp(port=port, patience=120.0)
    link.resume()
    print("running free for %.0fs before arming" % arm_after, flush=True)
    time.sleep(arm_after)
    link.interrupt()

    for at in wanted:
        link.stop_at(at)
    started = time.monotonic()
    print("armed %s" % ", ".join("0x%08x" % a for a in wanted), flush=True)
    print("window: from %.0fs to %.0fs of the run"
          % (arm_after, arm_after + watch), flush=True)

    hits = {a: 0 for a in wanted}
    other = 0
    deadline = started + watch
    while time.monotonic() < deadline:
        if not link.go(timeout=max(1.0, deadline - time.monotonic())):
            break
        regs = link.cmd("g")
        if not regs or len(regs) < 8:
            break
        pc = int.from_bytes(bytes.fromhex(regs[:8]), "little")
        if pc in hits:
            hits[pc] += 1
        else:
            other += 1
        # Keep going rather than stopping at the first hit: which of several
        # candidates is reached, and how often, is the interesting part.
        if sum(hits.values()) + other >= 200:
            break

    print("", flush=True)
    for at in wanted:
        print("0x%08x  %d hit(s)" % (at, hits[at]), flush=True)
    if other:
        print("%d stop(s) somewhere else" % other, flush=True)
    link.resume()
    link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
