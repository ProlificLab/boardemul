"""The guest's own clock, for the board's parts (ADR 0104).

QEMU's `esp-guest-clock` device writes its virtual time, eight bytes
little-endian in nanoseconds, to this socket every 10 ms of guest time. A part
that has to keep the board's time -- a receiver's fix a second -- asks
`seconds()` instead of reading the host's clock.

Given a quantum, every tick is a barrier instead (ADR 0113): the guest waits
until `on_barrier` has answered, and the answer names lines to move --
`(pin, level or None, rise_at_ns)` -- which the device drives on the guest's
clock. That is how boards on one air keep one time.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from pathlib import Path

log = logging.getLogger("clock")

FRAME = struct.Struct("<Q")
# A barrier's answer, as esp_guest_clock.c reads it: a count, then the lines.
COUNT = struct.Struct("<I")
LINE = struct.Struct("<IB3xQ")
LINE_LEFT = 0xFF


class GuestClock:
    def __init__(self, socket_path: str | Path, quantum_ns: int = 0,
                 on_barrier=None):
        self.socket_path = str(socket_path)
        self.quantum_ns = quantum_ns
        # Called with the guest's time at each barrier; returns the lines to
        # move. Set by the board once the parts it speaks for exist.
        self.on_barrier = on_barrier
        self._ns: int | None = None
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self.ticks = 0

    def device(self) -> str:
        """The -device spec that connects QEMU's side to this one."""
        spec = f"esp-guest-clock,socket={self.socket_path}"
        return spec + (f",quantum_ns={self.quantum_ns}" if self.quantum_ns else "")

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        threading.Thread(target=self._serve, name="clock", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def seconds(self) -> float | None:
        """The latest guest time heard, or None before the first tick."""
        ns = self._ns
        return None if ns is None else ns / 1e9

    def feed(self, data: bytes) -> bytes:
        """Take whole frames from `data`, keep the newest, return the rest."""
        whole = len(data) - len(data) % FRAME.size
        for at in range(0, whole, FRAME.size):
            (self._ns,) = FRAME.unpack_from(data, at)
            self.ticks += 1
        return data[whole:]

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        log.info("guest clock connected%s",
                 f", a barrier every {self.quantum_ns} ns" if self.quantum_ns else "")
        if self.quantum_ns:
            with conn:
                self._barriers(conn)
            return
        carry = b""
        with conn:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                carry = self.feed(carry + chunk)

    def _barriers(self, conn: socket.socket) -> None:
        """Each tick waits for its answer: the guest stands still until then."""
        while not self._stop.is_set():
            frame = _recv(conn, FRAME.size)
            if frame is None:
                return
            (self._ns,) = FRAME.unpack(frame)
            self.ticks += 1
            lines = []
            if self.on_barrier is not None:
                try:
                    lines = self.on_barrier(self._ns) or []
                except Exception:
                    log.exception("the barrier at %d ns raised; the guest goes on",
                                  self._ns)
                    lines = []
            answer = COUNT.pack(len(lines)) + b"".join(
                LINE.pack(pin, LINE_LEFT if level is None else int(bool(level)),
                          max(0, rise))
                for pin, level, rise in lines)
            try:
                conn.sendall(answer)
            except OSError:
                return


def _recv(conn: socket.socket, count: int) -> bytes | None:
    buf = b""
    while len(buf) < count:
        try:
            chunk = conn.recv(count - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf
