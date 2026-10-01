"""The guest's own clock, for the board's parts (ADR 0104).

QEMU's `esp-guest-clock` device writes its virtual time, eight bytes
little-endian in nanoseconds, to this socket every 10 ms of guest time. A part
that has to keep the board's time -- a receiver's fix a second -- asks
`seconds()` instead of reading the host's clock.
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


class GuestClock:
    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)
        self._ns: int | None = None
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self.ticks = 0

    def device(self) -> str:
        """The -device spec that connects QEMU's side to this one."""
        return f"esp-guest-clock,socket={self.socket_path}"

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
        log.info("guest clock connected")
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
