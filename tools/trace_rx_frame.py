# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Single-step one chosen received frame through the ROM's receive path.

The frame that matters is one in a hundred, so the naive form -- a breakpoint
at `wDev_ProcessRxSucData` and a filter at each stop -- does not work: the
stops slow the guest about fiftyfold, and a guest that slow never reaches an
authentication at all. One attempt got eight seconds into the firmware for ten
minutes of waiting.

So the device is asked to do the filtering. With BOARDEMUL_WIFI_MARK set to an
802.11 subtype, `esp_wifi_mac.c` raises a cell at its `+0x1ff8` when the
station transmits one and holds that answer back for half a second, which is
long enough for this to notice, arm one breakpoint, and catch the reply. No
breakpoints exist until the frame that matters does.

The hold is a delay rather than a handshake because a debugger cannot release
one: QEMU puts gdb's memory writes through `address_space_write_rom`, which
reaches memory-backed regions only, so a write to a device register from a
debugger is accepted and silently dropped. Reads are fine.

    python3 trace_rx_frame.py QMP_SOCKET GDB_PORT [STEPS] [--kind=b0]
                              [--at=0xADDR]

Each step prints the instruction and the registers *entering* it, so a
branch's condition is read off its own line rather than guessed at. This is
what found the authentication reply being handed to `wDev_DiscardFrame`
(ADR 0062).
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
import disas_rom  # noqa: E402
from rsp import Rsp  # noqa: E402

ENTRY = 0x40041184            # wDev_ProcessRxSucData, resolved through its stub
WDEV_CTRL_PTR = 0x3FCEF93C
MAC_BASE = 0x60033000
R_RX_LAST = 0x090
PROBE_GATE = 0x1ff8
RX_HEADER = 48
SYMBOLS = Path(__file__).resolve().parent.parent / "rom" / "symbols.txt"
# The images a PC can fall in, so a step can be printed as an instruction
# rather than an address. Dumped by wdev_poke.py; the ROM one ships with QEMU.
IMAGES = ((Path(__file__).resolve().parent.parent / "rom" / "rom.bin", 0x40000000),
          (Path(__file__).resolve().parent.parent / "poke" / "iram.bin", 0x40370000),
          (Path(__file__).resolve().parent.parent / "poke" / "irom.bin", 0x42000000))


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
    at, names = [], []
    if SYMBOLS.exists():
        rows = []
        for line in SYMBOLS.read_text(errors="replace").splitlines():
            found = re.match(r"(\S+)\s+stub 0x([0-9a-f]+)\s+code 0x([0-9a-f]+)", line)
            if found:
                rows.append((int(found.group(3), 16), found.group(1)))
        rows.sort()
        at = [r[0] for r in rows]
        names = [r[1] for r in rows]
    return at, names


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    cmd = qmp(args[0])
    port = int(args[1]) if len(args) > 1 else 1234
    steps = int(args[2]) if len(args) > 2 else 200
    entry = ENTRY
    for flag in flags:
        if flag.startswith("--at"):
            entry = int(flag.split("=", 1)[1], 16)
    poke = "--poke" in flags

    at, names = symbols()
    loaded = [(base, path.read_bytes()) for path, base in IMAGES if path.exists()]

    def text(pc: int) -> str:
        for base, blob in loaded:
            if base <= pc < base + len(blob) - 3:
                try:
                    return disas_rom.decode(pc, blob, pc - base)[0]
                except Exception:
                    return ""
        return ""

    def where(pc: int) -> str:
        if not at:
            return ""
        i = bisect.bisect_right(at, pc) - 1
        if i < 0 or pc - at[i] > 8192:
            return ""
        return "%s+%d" % (names[i], pc - at[i])

    def registers() -> tuple[int, dict[str, int]]:
        out = cmd("human-monitor-command",
                  **{"command-line": "info registers"}).get("return", "")
        pc = re.search(r"PC=([0-9a-f]{8})", out)
        regs = {name.lower(): int(value, 16)
                for name, value in re.findall(r"(A\d\d)=([0-9a-f]{8})", out)}
        return (int(pc.group(1), 16) if pc else 0), regs

    link = Rsp(port=port, patience=90.0)
    print("break at 0x%08x (%s): %s"
          % (entry, where(entry) or "?", link.stop_at(entry) or "(empty)"), flush=True)
    kind, after = None, 0.0
    for flag in flags:
        if flag.startswith("--kind"):
            kind = int(flag.split("=", 1)[1], 16)
        if flag.startswith("--after"):
            after = float(flag.split("=", 1)[1])

    # With BOARDEMUL_WIFI_MARK set, the device raises a cell in its own span
    # when the station sends that subtype and holds the answer back until this
    # clears it. So: no breakpoint at all while the guest runs to the frame
    # that matters -- a breakpoint on every frame slows it fifty-fold and it
    # never gets there -- then one stop, on the right frame, by construction.
    link.stop_at(entry, on=False)
    if kind is not None:
        print("waiting for the station to send a 0x%02x" % kind, flush=True)
        waited = 0.0
        while True:
            link.resume()
            time.sleep(0.2)
            waited += 0.2
            link.interrupt()
            gate = link.word(MAC_BASE + PROBE_GATE) & 0xFF
            if gate == kind:
                print("  it did, after %.0fs; arming and releasing the answer"
                      % waited, flush=True)
                break
            if waited > 600:
                print("it never did in %.0fs" % waited)
                link.close()
                return
        print("  breakpoint: %s" % (link.stop_at(entry) or "(empty)"), flush=True)
    else:
        link.stop_at(entry)
    stop = link.go(timeout=600.0)
    if not stop:
        print("the receive path never ran")
        link.interrupt()
        link.close()
        return
    if stop.startswith("W") or stop.startswith("X"):
        print("the guest exited before the frame arrived: %s" % stop)
        link.close()
        return
    print("stopped: %s" % stop, flush=True)
    pc, regs = registers()
    print("hit: pc=0x%08x %s" % (pc, where(pc)), flush=True)
    print("  a0=%08x (the caller, less its window increment: 0x%08x)"
          % (regs.get("a00", 0), regs.get("a00", 0) & 0x3FFFFFFF), flush=True)
    print("  a2=%08x a3=%08x a4=%08x a5=%08x a6=%08x a7=%08x"
          % tuple(regs.get("a%02d" % n, 0) for n in (2, 3, 4, 5, 6, 7)), flush=True)
    # Learn the gdb register numbering once, so a later script can skip QMP.
    print("  p0 reads %r" % link.cmd("p0"), flush=True)

    link.stop_at(entry, on=False)
    started = time.monotonic()
    trace: list[tuple[int, str, dict]] = []
    for _ in range(steps):
        link.cmd("s", timeout=30.0)
        pc, regs = registers()
        trace.append((pc, where(pc), regs))
    print("%d step(s) in %.1fs" % (len(trace), time.monotonic() - started), flush=True)

    # Every step, with a mark where the path did not simply fall through: a
    # conditional that was not taken looks like the next address, and one that
    # was looks like a jump, so the marks are the decisions.
    last = None
    for i, (pc, name, regs) in enumerate(trace):
        jump = last is not None and not (0 < pc - last <= 3)
        # The registers printed are the state *entering* this instruction, so
        # a branch's condition is read from its own line.
        print("  %-4d %s 0x%08x %-20s %-30s %s"
              % (i, "*" if jump else " ", pc, name, text(pc),
                 " ".join("a%d=%08x" % (n, regs.get("a%02d" % n, 0))
                          for n in (2, 3, 4, 5, 9, 10, 11, 13, 14))))
        last = pc
    link.close()


if __name__ == "__main__":
    main()
