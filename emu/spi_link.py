#!/usr/bin/env python3
"""Receive completed SPI transactions from the in-QEMU controller (ADR 0011).

The controller batches on chip-select and only sends a segment when a
transaction ends or the data/command line changes, so what arrives here is a
command followed by its payload -- not a stream of single bytes.

Segment header, matching esp_spi_master.c:

    u8 type   0 = write only, no reply    1 = duplex, reply of `len` expected
    u8 dc     level of the data/command pin while these bytes were clocked
    u16 pad
    u32 len
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from pathlib import Path

log = logging.getLogger("spi")

SEGMENT = struct.Struct("<BBHI")
SEG_WRITE = 0
SEG_DUPLEX = 1


class SpiDevice:
    """A device on one chip-select. `dc` is 0 for command, 1 for data."""

    name = "spi-device"

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        return None

    def flush(self) -> None:
        """Called once the run is over. A panel with nothing pending does
        nothing; one caught part-way through an update emits what it has."""


class SpiServer:
    """Listens for the controller and services it on its own thread.

    One connection, one device: the controller already filtered by chip-select,
    so everything arriving here is addressed to this device.
    """

    def __init__(self, socket_path: str | Path, device: SpiDevice):
        self.socket_path = str(socket_path)
        self.device = device
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.segments = 0
        self.bytes_in = 0

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        self._thread = threading.Thread(target=self._serve, name="spi", daemon=True)
        self._thread.start()

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

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self._conn = conn
        log.info("spi controller connected")
        while not self._stop.is_set():
            header = self._recv(conn, SEGMENT.size)
            if header is None:
                return
            kind, dc, _pad, length = SEGMENT.unpack(header)
            payload = self._recv(conn, length) if length else b""
            if payload is None:
                return
            self.segments += 1
            self.bytes_in += len(payload)
            try:
                reply = self.device.on_segment(dc, payload, kind == SEG_DUPLEX)
            except Exception:
                # A model bug must not wedge the guest mid-transaction.
                log.exception("spi device raised on a %d byte segment", length)
                reply = None
            if kind == SEG_DUPLEX:
                out = (reply or b"")[:length].ljust(length, b"\x00")
                try:
                    conn.sendall(out)
                except OSError:
                    return

    @staticmethod
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
