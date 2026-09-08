#!/usr/bin/env python3
"""Find the advertising data in a running guest's memory.

    python3 tools/find_ble_advertising.py QMP_SOCKET OUTDIR

Needs QEMU's monitor, which the runner does not open by default:

    BOARDEMUL_QEMU_ARGS="-qmp unix:/tmp/run/qmp.sock,server,nowait"         python3 emu/run.py ... --overlay 0xc90000=build/littlefs-bluetooth.bin

Dumps internal RAM and looks for what advertising data must contain -- the
name the interface advertises under, and the structures around it, each of
which is a length, a type and that many bytes. What it reports is where the
payload is and, as importantly, where it is not (ADR 0044).
"""
import json
import socket
import sys
import time
from pathlib import Path

SRAM_BASE, SRAM_SIZE = 0x3FC80000, 0x70000


def qmp(sock_path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(60):
        try:
            s.connect(str(sock_path))
            break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit(f"no QMP socket at {sock_path}")
    f = s.makefile("rwb")
    f.readline()

    def cmd(execute, **arguments):
        f.write(json.dumps({"execute": execute, "arguments": arguments}).encode() + b"\n")
        f.flush()
        while True:
            line = f.readline()
            if not line:
                raise SystemExit("QMP closed")
            reply = json.loads(line)
            if "return" in reply or "error" in reply:
                return reply

    cmd("qmp_capabilities")
    return cmd


def main() -> int:
    sock_path, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = qmp(sock_path)

    dump = outdir / "sram.bin"
    reply = cmd("pmemsave", val=SRAM_BASE, size=SRAM_SIZE, filename=str(dump))
    if "error" in reply:
        raise SystemExit(f"pmemsave refused: {reply}")
    data = dump.read_bytes()
    print(f"dumped {len(data)} bytes from 0x{SRAM_BASE:08x}")

    # The name the interface advertises under is the plainest thing to find.
    for needle in (b"Meshtastic", b"Mesh"):
        hits = []
        start = 0
        while True:
            at = data.find(needle, start)
            if at < 0:
                break
            hits.append(at)
            start = at + 1
        print(f"{needle.decode()}: {len(hits)} hit(s)"
              + (f", first at 0x{SRAM_BASE + hits[0]:08x}" if hits else ""))
        for at in hits[:6]:
            around = data[max(0, at - 48):at + 48]
            print(f"    0x{SRAM_BASE + at:08x}  {around.hex(' ')}")
        # The advertising payload announces itself: an AD structure is a
        # length, a type, and that many bytes. 0x09 is the complete local name.
        for at in hits:
            for back in range(2, 40):
                if at - back < 0:
                    break
                if data[at - back] == 0x09 and data[at - back - 1] == back:
                    start = at - back - 1
                    print(f"  advertising data at 0x{SRAM_BASE + start:08x}: "
                          f"{data[start:start + 40].hex(' ')}")
                    print(f"    the 32 bytes before it: "
                          f"{data[max(0, start - 32):start].hex(' ')}")
                    break
        if hits:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
