#!/usr/bin/env python3
"""Serve completed I2C transactions from the in-QEMU controller (ADR 0011).

The controller assembles a whole transaction -- address, everything written to
it, and how many bytes are wanted back -- and sends it once. What arrives here
is what a logic analyser would show, not the twenty-four register accesses the
driver used to build it.

Request, matching esp_i2c_master.c:

    u8  addr    seven-bit device address
    u8  flags   bit 0: the driver asked for the acknowledgement to be checked
    u16 wlen
    u16 rlen
    u16 pad
    ... wlen bytes written to the device

Reply:

    u8 ack      zero means nobody answered
    u8 pad[3]
    ... rlen bytes read back
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from collections import deque
from pathlib import Path

from emu.peripherals.i2c import I2CDevice

log = logging.getLogger("i2c")

REQUEST = struct.Struct("<BBHHH")
REPLY = struct.Struct("<BBBB")


class I2CServer:
    """One bus, one connection, serviced on its own thread."""

    def __init__(self, socket_path: str | Path, devices: list[I2CDevice],
                 name: str = "i2c0"):
        self.socket_path = str(socket_path)
        self.name = name
        self.devices: dict[int, I2CDevice] = {d.address: d for d in devices}
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self.transactions = 0
        self.acks: set[int] = set()
        self.nacks: set[int] = set()
        # Guessing which model returned a bad value is slow; reading the last
        # few transactions off the wire is not.
        self.history: deque[tuple[int, bytes, bytes]] = deque(maxlen=64)

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

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self._conn = conn
        log.info("%s controller connected, %d device(s)", self.name, len(self.devices))
        while not self._stop.is_set():
            header = self._recv(conn, REQUEST.size)
            if header is None:
                return
            addr, flags, wlen, rlen, _pad = REQUEST.unpack(header)
            written = self._recv(conn, wlen) if wlen else b""
            if written is None:
                return
            self.transactions += 1
            try:
                ack, data = self._dispatch(addr, written, rlen)
            except Exception:
                # A model bug must not wedge the guest mid-transaction.
                log.exception("device 0x%02x raised", addr)
                ack, data = False, bytes(rlen)
            self.history.append((addr, written, data))
            try:
                conn.sendall(REPLY.pack(1 if ack else 0, 0, 0, 0) + data)
            except OSError:
                return

    def _dispatch(self, addr: int, written: bytes, rlen: int) -> tuple[bool, bytes]:
        device = self.devices.get(addr)
        if device is None:
            self.nacks.add(addr)
            return False, bytes(rlen)
        self.acks.add(addr)

        acked = True
        if written:
            device.start(reading=False)
            acked = device.write(written)
        data = b""
        if rlen:
            # A repeated START for the read half; the device snapshots its reply
            # here, which is what keeps a split read coherent (ADR 0006).
            device.start(reading=True)
            data = device.read(rlen)
        else:
            device.stop()
        return acked, data.ljust(rlen, b"\xff")[:rlen]

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
