#!/usr/bin/env python3
"""Drive GPIO input pins from the host.

The GPIO block itself moved into QEMU (ADR 0011): the SPI controller has to read
chip-select and data/command on every transfer, which cannot cross a socket, and
its interrupt path has to fire while the guest is busy elsewhere.

What is left on this side is the ability to drive a line that has no driver in
the machine -- a touch controller's interrupt, a panel's BUSY pin. One message,
two words, matching esp_gpio_block.c:

    u32 pin
    u32 level
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from pathlib import Path

log = logging.getLogger("gpio")

MESSAGE = struct.Struct("<II")


class GpioClient:
    """Listens for the in-QEMU GPIO block and pushes pin levels to it.

    Levels set before the machine connects are not lost: they are replayed once
    it does, so a pin that must idle high does so from the first instruction the
    guest executes rather than from whenever the host got around to it.
    """

    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._lock = threading.Lock()
        self._levels: dict[int, int] = {}
        self.sent = 0

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        threading.Thread(target=self._accept, name="gpio", daemon=True).start()

    def _accept(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with self._lock:
            self._conn = conn
            pending = list(self._levels.items())
        log.info("gpio block connected; replaying %d level(s)", len(pending))
        for pin, level in pending:
            self._send(pin, level)

    def _send(self, pin: int, level: int) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.sendall(MESSAGE.pack(pin, level))
                self.sent += 1
            except OSError:
                self._conn = None

    def drive(self, pin: int, level: int) -> None:
        level = 1 if level else 0
        with self._lock:
            if self._levels.get(pin) == level:
                return
            self._levels[pin] = level
        self._send(pin, level)

    def pulse_low(self, pin: int) -> None:
        """Guarantee one falling edge whatever the line was doing before.

        A driver whose handler only sets a flag, and whose read path returns
        early unless that flag is set, never collects anything without an edge.
        """
        self.drive(pin, 1)
        self.drive(pin, 0)
        self.drive(pin, 1)

    def stop(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
        if self._listener:
            self._listener.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
