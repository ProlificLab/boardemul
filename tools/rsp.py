#!/usr/bin/env python3
"""A minimal GDB remote-serial-protocol client, stdlib only.

QEMU's monitor can read guest memory and cannot write it, and it has no
watchpoints. Its gdbstub has both. Nothing here needs a gdb binary -- the
protocol is a length-free packet framing over a socket, and the four packets
this project wants (read memory, write memory, insert watchpoint, continue)
are one line each.

Attaching stops the machine, which is a fact worth knowing rather than a
defect: a script that attaches and never continues has hung the guest.
"""
from __future__ import annotations

import socket
import time


def frame(data: str) -> bytes:
    body = data.encode()
    return b"$" + body + b"#" + ("%02x" % (sum(body) & 0xFF)).encode()


class Rsp:
    def __init__(self, host: str = "127.0.0.1", port: int = 1234,
                 timeout: float = 30.0, patience: float = 0.0) -> None:
        # The stub is opened when the machine is created, which is after the
        # flash image is built, so a script that wants to be there from the
        # start has to wait for the port rather than assume it.
        started = time.monotonic()
        while True:
            try:
                self.sock = socket.create_connection((host, port), timeout=timeout)
                break
            except OSError:
                if time.monotonic() - started > patience:
                    raise
                time.sleep(0.5)
        self.sock.settimeout(timeout)
        self.rest = b""

    # -- framing ----------------------------------------------------------
    def _take(self, n: int) -> bytes:
        while len(self.rest) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("gdbstub closed the connection")
            self.rest += chunk
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def _packet(self, timeout: float | None = None) -> str:
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            head = self._take(1)
            if head == b"$":
                break
            # '+' and '-' are acknowledgements; anything else is noise.
        body = b""
        while True:
            byte = self._take(1)
            if byte == b"#":
                break
            body += byte
        self._take(2)
        self.sock.sendall(b"+")
        return body.decode(errors="replace")

    def cmd(self, data: str, timeout: float | None = None) -> str:
        self.sock.sendall(frame(data))
        return self._packet(timeout)

    # -- the four things this is for ---------------------------------------
    def read(self, at: int, length: int) -> bytes:
        reply = self.cmd("m%x,%x" % (at, length))
        if reply.startswith("E") or len(reply) < length * 2:
            raise RuntimeError("read 0x%08x failed: %r" % (at, reply))
        return bytes.fromhex(reply[: length * 2])

    def word(self, at: int) -> int:
        return int.from_bytes(self.read(at, 4), "little")

    def write(self, at: int, data: bytes) -> None:
        reply = self.cmd("M%x,%x:%s" % (at, len(data), data.hex()))
        if reply != "OK":
            raise RuntimeError("write 0x%08x failed: %r" % (at, reply))

    def watch(self, at: int, length: int, on: bool = True) -> str:
        # Z2 is a write watchpoint in gdb's own numbering; z2 removes it.
        return self.cmd("%s2,%x,%x" % ("Z" if on else "z", at, length))

    def stop_at(self, at: int, on: bool = True) -> str:
        """A code breakpoint. Unlike a watchpoint this costs the guest almost
        nothing until it fires, because it is checked per translated block."""
        return self.cmd("%s0,%x,1" % ("Z" if on else "z", at))

    def go(self, timeout: float = 60.0) -> str:
        """Continue, and return the stop reply. Empty string on a timeout,
        with the guest still running."""
        self.sock.sendall(frame("c"))
        try:
            return self._packet(timeout)
        except socket.timeout:
            return ""

    def resume(self) -> None:
        """Continue without waiting for the next stop, for a run that is being
        left alone rather than single-stepped."""
        self.sock.sendall(frame("c"))

    def interrupt(self, timeout: float = 10.0) -> str:
        self.sock.sendall(b"\x03")
        try:
            return self._packet(timeout)
        except socket.timeout:
            return ""

    def close(self) -> None:
        try:
            self.sock.sendall(frame("D"))
        except OSError:
            pass
        self.sock.close()
