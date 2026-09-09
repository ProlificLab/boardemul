#!/usr/bin/env python3
"""Put a model on the other end of one of the machine's UARTs.

Unlike the I2C and SPI buses, a UART needs nothing added to QEMU: the fork's
own controller already speaks to a character device, so a socket is enough and
this costs no C at all (ADR 0001). QEMU connects to the socket named here, and
from then on the two sides exchange plain bytes in both directions, which is
what a serial line is.

A part on a serial line is not asked for anything -- it talks when it has
something to say and answers when spoken to -- so a device here gets both a
chance to react to what arrived and a chance to speak unprompted.
"""

from __future__ import annotations

import logging
import os
import selectors
import socket
import threading
from pathlib import Path

log = logging.getLogger("serial")

# How often a device is offered the chance to say something unprompted. Fine
# enough that a part reporting once a second is never more than this late.
TICK_SECONDS = 0.05


class SerialDevice:
    """What a part on a serial line has to be able to do."""

    def receive(self, data: bytes) -> bytes:
        """Bytes arrived from the guest; return whatever answers them."""
        return b""

    def poll(self) -> bytes:
        """Called regularly; return whatever the part says on its own."""
        return b""


class SerialServer:
    """One UART's far end, serviced on its own thread."""

    def __init__(self, socket_path: str | Path, device: SerialDevice,
                 name: str = "uart"):
        self.socket_path = str(socket_path)
        self.name = name
        self.device = device
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self.bytes_in = 0
        self.bytes_out = 0

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        threading.Thread(target=self._serve, name=self.name, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._conn, self._listener):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def _send(self, conn: socket.socket, data: bytes) -> bool:
        if not data:
            return True
        try:
            conn.sendall(data)
        except OSError:
            return False
        self.bytes_out += len(data)
        return True

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self._conn = conn
        log.info("%s connected to %s", self.name, type(self.device).__name__)
        sel = selectors.DefaultSelector()
        sel.register(conn, selectors.EVENT_READ)
        while not self._stop.is_set():
            for _key, _mask in sel.select(TICK_SECONDS):
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                self.bytes_in += len(data)
                try:
                    answer = self.device.receive(data)
                except Exception:
                    # A model bug must not take the guest's serial line down.
                    log.exception("%s device raised on %d byte(s)", self.name, len(data))
                    answer = b""
                if not self._send(conn, answer):
                    return
            try:
                unprompted = self.device.poll()
            except Exception:
                log.exception("%s device raised while speaking", self.name)
                unprompted = b""
            if not self._send(conn, unprompted):
                return
