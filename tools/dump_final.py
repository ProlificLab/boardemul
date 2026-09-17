#!/usr/bin/env python3
"""One run, all three: the key, the message, and the firmware's MIC output.

Break at 0x4206e194 (the compare). a10=a1+124 holds the received MIC, a11 is
frame+81 where the firmware wrote its computed MIC. Read the key context
(*0x4203f500 = 0x3fca70c4, 16 bytes), the frame (a11-81), and both MICs, so
HMAC(key, frame-with-MIC-zeroed) can be checked offline against what the
firmware computed -- in a single run, no cross-run assumptions.

    python3 logs/dump_final.py QMP_SOCKET GDB_PORT
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rsp import Rsp  # noqa: E402

E194 = 0x4206E194
KEY_CTX = 0x3FCA70C4
AP = bytes.fromhex("020000000001")
STA = bytes.fromhex("0254444d0001")


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


def regs(cmd):
    out = cmd("human-monitor-command",
              **{"command-line": "info registers"}).get("return", "")
    return {n.lower(): int(v, 16) for n, v in re.findall(r"(A\d\d)=([0-9a-f]{8})", out)}


def main() -> None:
    cmd = qmp(sys.argv[1])
    link = Rsp(port=int(sys.argv[2]), patience=90.0)
    link.resume()
    print("running free 80s...", flush=True)
    time.sleep(80)
    link.interrupt()
    link.stop_at(E194)
    for hit in range(2):
        stop = link.go(timeout=180.0)
        if not stop or stop[:1] in ("W", "X"):
            print("no hit: %r" % stop, flush=True)
            break
        r = regs(cmd)
        a10, a11 = r.get("a10", 0), r.get("a11", 0)
        received = link.read(a10, 16)
        computed = link.read(a11, 16)
        key = link.read(KEY_CTX, 16)
        frame = link.read(a11 - 81, 155)
        zeroed = frame[:81] + bytes(16) + frame[97:]
        correct = hmac.new(key, zeroed, hashlib.sha1).digest()[:16]
        print("\n=== hit %d" % hit, flush=True)
        print("  key (from 0x3fca70c4):     %s" % key.hex(), flush=True)
        print("  received MIC (a10):        %s" % received.hex(), flush=True)
        print("  firmware computed (a11):   %s" % computed.hex(), flush=True)
        print("  HMAC-SHA1(key, frame):     %s" % correct.hex(), flush=True)
        print("  correct == received? %s   correct == firmware? %s"
              % (correct == received, correct == computed), flush=True)
        print("  --> the firmware %s the standard HMAC with its own key+message"
              % ("MATCHES" if correct == computed else "MISCOMPUTES"), flush=True)
    link.stop_at(E194, on=False)
    link.close()


if __name__ == "__main__":
    main()
