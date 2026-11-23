#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Dump the two buffers the message-three MIC comparison compares.

At 0x4206e194 the supplicant calls memcmp(a10, a11, a12) and bails at
0x4206e197 if it differs; a12 is 16, a10 and a11 are the received and computed
MIC (ADR 0080). This breaks there, reads a10/a11/a12 from the monitor, and
reads the sixteen bytes at each so the two MICs can be seen side by side --
proving they are MICs and showing how they differ.

The breakpoint only fires for a MIC-bearing frame, which the station receives
only as message three, so it is not hot. Per the debugger notes: connecting
stops the guest, so resume before sleeping; arm the breakpoint only after the
guest is past its scan and into the handshake.

    python3 logs/dump_mic.py QMP_SOCKET GDB_PORT
"""
from __future__ import annotations

import json
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rsp import Rsp  # noqa: E402

E194 = 0x4206E15C            # the memcpy that READS frame+81 (delivered MIC), before any compute
MIC_OFFSET = 81             # where the MIC sits inside the EAPOL frame


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


def registers(cmd) -> tuple[int, dict[str, int]]:
    out = cmd("human-monitor-command",
              **{"command-line": "info registers"}).get("return", "")
    pc = re.search(r"PC=([0-9a-f]{8})", out)
    regs = {name.lower(): int(value, 16)
            for name, value in re.findall(r"(A\d\d)=([0-9a-f]{8})", out)}
    return (int(pc.group(1), 16) if pc else 0), regs


def main() -> None:
    qmp_path, port = sys.argv[1], int(sys.argv[2])
    cmd = qmp(qmp_path)
    link = Rsp(port=port, patience=90.0)

    # Connecting stopped the guest. Let it run, free of breakpoints, into the
    # handshake before arming anything -- a breakpoint during the scan stops it
    # reaching an association at all.
    link.resume()
    warm = 80
    print("running free for %ds to reach the handshake..." % warm, flush=True)
    time.sleep(warm)
    link.interrupt()
    print("arming the breakpoint at 0x%08x: %s"
          % (E194, link.stop_at(E194) or "(empty)"), flush=True)

    for hit in range(4):
        stop = link.go(timeout=180.0)
        if not stop:
            print("no message-three MIC check in the window", flush=True)
            break
        if stop[:1] in ("W", "X"):
            print("guest exited: %s" % stop, flush=True)
            break
        pc, regs = registers(cmd)
        a10, a11, a12 = regs.get("a10", 0), regs.get("a11", 0), regs.get("a12", 0)
        print("\n=== hit %d: pc=0x%08x (expect 0x4206e194)" % (hit, pc), flush=True)
        print("  at e15c memcpy(dst a10=0x%08x, SRC a11=frame+81=0x%08x, len=%d)"
              % (a10, a11, a12), flush=True)
        try:
            src = link.read(a11, 16)   # the DELIVERED MIC, before any compute
            print("  DELIVERED MIC (frame+81, before compute): %s" % src.hex(),
                  flush=True)
            print("  peer sent 6b1c...; if this is 6b1c the delivery is CLEAN "
                  "(firmware computes nonstandard); else RX corrupted it",
                  flush=True)
            b10, b11 = src, src
            # a11 is frame+81 (the MIC field in the frame); dump the frame
            # around it so the whole EAPOL frame can be compared to what the
            # peer sent.
            frame_at = a11 - MIC_OFFSET
            frame = link.read(frame_at, 160)
            print("  frame@0x%08x (a11-81): %s" % (frame_at, frame.hex()),
                  flush=True)
        except Exception as exc:
            print("  read failed: %r" % exc, flush=True)

    link.stop_at(E194, on=False)
    link.close()


if __name__ == "__main__":
    main()
