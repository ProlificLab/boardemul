# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""What the receive ring and both cores are doing after a chosen frame.

Written to answer one question -- why the driver stops picking frames up for a
second or more after it associates (ADR 0064) -- and kept because the shape is
reusable: wait for the station to transmit a subtype, then watch the ring and
the cores rather than stepping anything.

`BOARDEMUL_WIFI_MARK=<subtype>` makes the device raise a cell at its `+0x1ff8`
when the station transmits that subtype. This polls the cell with no
breakpoints at all -- a breakpoint would change the timing this is measuring --
and once it flips, takes snapshots of:

  * the receive list head out of `*wDevCtrl`, and each descriptor's word, owner
    bit, end-of-frame bit and buffer;
  * the MAC's interrupt status, receive enable and hold registers;
  * and once, mid-window, both cores' PC, PS and INTENABLE.

What it found, the first time it ran: the interrupt asserted and never
acknowledged, three descriptors free, and both cores spinning at interrupt
level three in ESP-IDF's spinlock.

    python3 ring_state.py QMP_SOCKET GDB_PORT [SECONDS]
"""
from __future__ import annotations

import bisect
import json
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from rsp import Rsp  # noqa: E402

MAC_BASE = 0x60033000
PROBE_GATE = 0x1FF8
SYMBOLS = Path(__file__).resolve().parent.parent / "rom" / "symbols.txt"

# Where a program counter can fall, so a sample can be named by region even
# when there is no symbol for it.
REGIONS = (
    (0x40000000, 0x40070000, "ROM"),
    (0x40374000, 0x403875d4, "IRAM (driver)"),
    (0x3FC80000, 0x3FD00000, "DRAM"),
    (0x42000020, 0x42124198, "flash text"),
    (0x3C130020, 0x3C1ABA10, "flash data"),
)


def qmp(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    handle = sock.makefile("rwb", buffering=0)
    json.loads(handle.readline())

    def cmd(execute, **arguments):
        handle.write((json.dumps({"execute": execute,
                                  "arguments": arguments}) + "\n").encode())
        while True:
            msg = json.loads(handle.readline())
            if "return" in msg or "error" in msg:
                return msg
    cmd("qmp_capabilities")
    return cmd


def symbols() -> tuple[list[int], list[str]]:
    if not SYMBOLS.exists():
        return [], []
    pairs = []
    for line in SYMBOLS.read_text(errors="replace").splitlines():
        found = re.match(r"^\s*(?:0x)?([0-9a-fA-F]{8})\s+(\S+)", line)
        if found:
            pairs.append((int(found.group(1), 16), found.group(2)))
    pairs.sort()
    return [a for a, _ in pairs], [n for _, n in pairs]


def main() -> None:
    cmd = qmp(sys.argv[1])
    port = int(sys.argv[2])
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 2.5
    at, names = symbols()

    def where(pc: int) -> str:
        for low, high, name in REGIONS:
            if low <= pc < high:
                region = name
                break
        else:
            region = "?"
        if at:
            i = bisect.bisect_right(at, pc) - 1
            if 0 <= i and pc - at[i] < 4096:
                return "%s (%s+%d)" % (region, names[i], pc - at[i])
        return region

    def sample() -> tuple[int, int]:
        """Both cores' program counters, and the guest clock in milliseconds."""
        out = cmd("human-monitor-command",
                  **{"command-line": "info registers -a"}).get("return", "")
        pcs = [int(m, 16) for m in re.findall(r"PC=([0-9a-f]{8})", out)]
        return pcs, 0

    link = Rsp(port=port, patience=120.0)
    print("waiting for the station to authenticate", flush=True)
    waited = 0.0
    while True:
        link.resume()
        time.sleep(0.2)
        link.interrupt()
        if (link.word(MAC_BASE + PROBE_GATE) & 0xFF) == 0xB0:
            break
        waited += 0.2
        if waited > 600:
            print("it never did")
            link.close()
            return
    print("  it did, after %.0fs; sampling for %.1fs" % (waited, seconds),
          flush=True)

    # The ring, not the program counter. The cores turned out to be idle
    # through the window -- the driver is waiting, not computing -- so the
    # question is what state it left the receive list in.
    WDEV = 0x3FCEF93C
    print("")
    print("  snapshot  head        descriptors "
          "(addr: word0 own eof buffer)")
    for shot in range(24):
        control = link.word(WDEV)
        head = link.word(control) if control else 0
        row = []
        at = head
        for _ in range(4):
            if not at:
                break
            word = link.word(at)
            buf = link.word(at + 4)
            row.append("%08x:%08x %s%s buf=%08x"
                       % (at, word,
                          "O" if word & (1 << 31) else "-",
                          "E" if word & (1 << 30) else "-", buf))
            at = link.word(at + 8)
            if at == head:
                break
        # What the device thinks: an interrupt pending, and whether the
        # driver has its receiver switched on to hear anything at all.
        status = link.word(MAC_BASE + 0xC3C)
        enable = link.word(MAC_BASE + 0x0D8)
        hold = link.word(MAC_BASE + 0xCA0)
        print("  %8d  head %08x  status %08x rx %08x hold %08x  %s"
              % (shot, head, status, enable, hold, " | ".join(row)), flush=True)
        if shot == 8:
            # Once, in the middle of the window: everything the cores will
            # say. If the interrupt is asserted and unhandled, the reason is
            # in here -- the level a core is running at, and which sources it
            # has enabled.
            out = cmd("human-monitor-command",
                      **{"command-line": "info registers -a"}).get("return", "")
            print("--- both cores, mid-window ---", flush=True)
            for line in out.splitlines():
                if re.search(r"PC=|PS=|INTENABLE|INTERRUPT|CPU#", line):
                    print("    " + line.strip(), flush=True)
            print("--- end ---", flush=True)
        link.resume()
        time.sleep(seconds / 24)
        link.interrupt()
    link.resume()
    link.close()


if __name__ == "__main__":
    main()
