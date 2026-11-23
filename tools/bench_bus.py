#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Measure the host side of the MMIO bridge in isolation.

QEMU blocks on every access, so what matters is round-trip latency, not
throughput. This drives the bus server from a plain socket with no emulator
involved, which separates our own cost from anything QEMU or the container is
doing.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from emu.bus import REQUEST, RESPONSE, BusServer, Region  # noqa: E402

BASE = 0x60013000


class NullRegion(Region):
    """Does nothing, so the number measured is the transport and dispatch cost
    rather than any particular model's."""

    def read(self, offset: int, size: int) -> int:
        return 0


CLIENT = """
import socket, struct, sys, time
REQUEST = struct.Struct("<BBHII"); RESPONSE = struct.Struct("<I")
path, count, addr, mode = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(path)
rd = REQUEST.pack(0, 4, 0, addr, 0)
wr = REQUEST.pack(1, 4, 0, addr, 0x1234)
for _ in range(2000):
    c.sendall(rd); c.recv(RESPONSE.size)
t = time.perf_counter()
if mode == "read":
    for _ in range(count):
        c.sendall(rd); c.recv(RESPONSE.size)
else:
    # Writes get no reply. One read at the end proves they all landed: the
    # socket is FIFO, so its answer cannot arrive before they are processed.
    for _ in range(count):
        c.sendall(wr)
    c.sendall(rd); c.recv(RESPONSE.size)
print(time.perf_counter() - t)
"""


def bench(count: int, regions: int, socket_path: Path) -> None:
    # Several regions, because the server resolves an address to a region on
    # every access and that lookup is part of what is being measured.
    windows = [NullRegion(f"r{i}", BASE + i * 0x1000, 0x1000) for i in range(regions)]
    server = BusServer(socket_path, windows)
    server.start()

    # The client runs in its own process on purpose. Driving the server from a
    # thread in this one makes the two contend for the GIL and measures a
    # ping-pong QEMU never performs.
    addr = windows[-1].base + 0x40
    for mode in ("read", "write"):
        out = subprocess.run([sys.executable, "-c", CLIENT,
                              str(socket_path), str(count), str(addr), mode],
                             capture_output=True, text=True)
        if out.returncode != 0:
            print(out.stderr)
            server.stop()
            raise SystemExit("benchmark client failed")
        elapsed = float(out.stdout.strip())
        print(f"  regions {regions:2d} {mode:5s}: {count / elapsed:9,.0f} access/s   "
              f"{elapsed / count * 1e6:6.1f} us/access")
    server.stop()


def floor(count: int, socket_path: Path) -> None:
    """The cheapest possible server: blocking recv, blocking send, no selector
    and no dispatch. Anything above this number is ours to remove; anything at
    it is the environment and no amount of Python tuning will help."""
    import threading

    if socket_path.exists():
        socket_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    stop = threading.Event()

    def serve() -> None:
        conn, _ = listener.accept()
        reply = RESPONSE.pack(0)
        while not stop.is_set():
            data = conn.recv(REQUEST.size)
            if not data:
                return
            conn.sendall(reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    out = subprocess.run([sys.executable, "-c", CLIENT,
                          str(socket_path), str(count), str(BASE), "read"],
                         capture_output=True, text=True)
    stop.set()
    listener.close()
    socket_path.unlink(missing_ok=True)
    if out.returncode != 0:
        print(out.stderr)
        raise SystemExit("floor client failed")
    elapsed = float(out.stdout.strip())
    print(f"  echo floor : {count / elapsed:9,.0f} access/s   "
          f"{elapsed / count * 1e6:6.1f} us/access")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", "--count", type=int, default=40000)
    ap.add_argument("--socket", type=Path, default=Path("/tmp/bench.sock"))
    args = ap.parse_args(argv)

    print(f"round trips: {args.count}")
    floor(args.count, args.socket)
    for regions in (1, 12):
        bench(args.count, regions, args.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
