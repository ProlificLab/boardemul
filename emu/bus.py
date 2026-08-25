#!/usr/bin/env python3
"""The host side of the MMIO bridge (ADR 0005).

QEMU connects one socket per intercepted window and blocks on every access, so
the only hard requirement here is that a reply always goes back. A model that
raises would otherwise hang the guest forever rather than fail visibly.
"""

from __future__ import annotations

import logging
import os
import selectors
import socket
import struct
import threading
from collections import deque
from pathlib import Path

log = logging.getLogger("bus")

# u8 op | u8 size | u16 pad | u32 addr | u32 value  -- must match esp_bus_bridge.c
REQUEST = struct.Struct("<BBHII")
RESPONSE = struct.Struct("<II")   # value, desired irq level
IRQ_MSG = struct.Struct("<II")    # interrupt source, level

OP_READ = 0
OP_WRITE = 1


class Region:
    """An MMIO window handled in Python. Subclasses implement read/write in
    terms of an offset from `base`."""

    def __init__(self, name: str, base: int, size: int, priority: int = 100,
                 irq: int = -1):
        self.name = name
        self.base = base
        self.size = size
        self.priority = priority
        self.irq = irq

    def irq_level(self) -> int:
        """Level this peripheral wants on its interrupt line right now. Sampled
        on every access and delivered on the same round trip, so a completion
        the guest itself caused is visible before it can sleep."""
        return 0

    def read(self, offset: int, size: int) -> int:
        return 0

    def write(self, offset: int, value: int, size: int) -> None:
        pass

    def qemu_device_arg(self, socket_path: str) -> str:
        return (f"esp-bus-bridge,socket={socket_path},name={self.name},"
                f"base=0x{self.base:x},size=0x{self.size:x},"
                f"priority={self.priority},irq={self.irq}")

    def __repr__(self) -> str:
        return f"<{self.name} 0x{self.base:08x}+0x{self.size:x}>"


class BusServer:
    """Accepts one connection per bridge instance and services them until
    stopped. Runs on its own thread so the caller can supervise QEMU."""

    def __init__(self, socket_path: str | Path, regions: list[Region]):
        self.socket_path = str(socket_path)
        self.regions = sorted(regions, key=lambda r: r.base)
        self._sel = selectors.DefaultSelector()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.accesses = 0
        self.unclaimed = 0
        # A stalled guest polls one register forever, so the tail of this ring
        # names the register it is waiting on.
        self.recent: deque[tuple[int, int, int, int]] = deque(maxlen=64)
        # Called before servicing each access. This is how asynchronous events
        # reach device state without a second thread touching the models
        # (ADR 0010 decision 6).
        self.pumps: list = []

    def _find(self, addr: int) -> Region | None:
        for r in self.regions:
            if r.base <= addr < r.base + r.size:
                return r
        return None

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(16)
        self._listener.setblocking(False)
        self._sel.register(self._listener, selectors.EVENT_READ, self._accept)
        self._thread = threading.Thread(target=self._serve, name="bus", daemon=True)
        self._thread.start()
        log.info("bus listening on %s with %d region(s)",
                 self.socket_path, len(self.regions))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._listener:
            self._listener.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def _accept(self, listener: socket.socket) -> None:
        conn, _ = listener.accept()
        conn.setblocking(True)
        # Nagle would batch our 4-byte replies and stall the guest for
        # milliseconds per access. Unix sockets ignore TCP_NODELAY, but the
        # blocking-read loop below is what actually keeps latency down.
        self._sel.register(conn, selectors.EVENT_READ, self._service)

    def _service(self, conn: socket.socket) -> None:
        raw = self._recv_exact(conn, REQUEST.size)
        if raw is None:
            self._sel.unregister(conn)
            conn.close()
            return
        op, size, _pad, addr, value = REQUEST.unpack(raw)
        self.accesses += 1
        for pump in self.pumps:
            try:
                pump()
            except Exception:
                log.exception("input pump raised")

        region = self._find(addr)
        result = 0
        irq = 0
        _dbg = log.isEnabledFor(logging.DEBUG)
        if region is None:
            self.unclaimed += 1
            log.warning("access to unclaimed 0x%08x (op=%d size=%d)", addr, op, size)
        else:
            offset = addr - region.base
            try:
                if op == OP_READ:
                    result = region.read(offset, size) & 0xFFFFFFFF
                else:
                    region.write(offset, value, size)
            except Exception:
                # A model bug must not become a guest hang. Log it and answer.
                log.exception("model raised on %s 0x%08x",
                              "read" if op == OP_READ else "write", addr)
            irq = region.irq_level()
        self.recent.append((op, addr, size, result if op == OP_READ else value))
        if _dbg and region is not None:
            log.debug("%s %s +0x%03x = 0x%08x irq=%d", region.name,
                      "rd" if op == OP_READ else "wr", addr - region.base,
                      result if op == OP_READ else value, irq)
        try:
            conn.sendall(RESPONSE.pack(result, irq))
        except OSError:
            self._sel.unregister(conn)
            conn.close()

    @staticmethod
    def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _serve(self) -> None:
        while not self._stop.is_set():
            for key, _ in self._sel.select(timeout=0.2):
                key.data(key.fileobj)

    def qemu_args(self, irq_socket: str | None = None) -> list[str]:
        args: list[str] = []
        for i, r in enumerate(self.regions):
            arg = r.qemu_device_arg(self.socket_path)
            # Only one instance needs to open the interrupt channel; it can
            # drive any source, so the rest do not duplicate it.
            if irq_socket and i == 0:
                arg += f",irqsock={irq_socket}"
            args += ["-device", arg]
        return args


class IrqChannel:
    """Out-of-band interrupt delivery (ADR 0010, corrected).

    A level returned on the request/response path only reaches the guest when it
    next accesses that same window. For a peripheral the guest polls -- I2C, SPI
    -- that is fine, because its own access is what raised the interrupt. For one
    it *waits* on, it is a deadlock: the handler that would touch the window only
    runs once the interrupt has been delivered.

    This channel breaks that cycle. QEMU reads it from its main loop, so a level
    can be raised at any moment regardless of what the guest is doing.
    """

    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._levels: dict[int, int] = {}
        self._lock = threading.Lock()
        self.sent = 0

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        threading.Thread(target=self._accept, name="irq", daemon=True).start()

    def _accept(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with self._lock:
            self._conn = conn
        log.info("interrupt channel connected")

    def set(self, source: int, level: int) -> None:
        level = 1 if level else 0
        with self._lock:
            if self._levels.get(source) == level or self._conn is None:
                self._levels[source] = level
                return
            self._levels[source] = level
            try:
                self._conn.sendall(IRQ_MSG.pack(source, level))
                self.sent += 1
            except OSError:
                self._conn = None

    def stop(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
        if self._listener:
            self._listener.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
