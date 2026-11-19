#!/usr/bin/env python3
"""What a person types into the chip's serial ports (ADR 0156).

Each port reads a named pipe in the run's directory, given to QEMU as the
`input-path` of its `file` chardev: `uart.in` for UART0, `usb.in` for the USB
Serial/JTAG controller (ADR 0100). This holds each pipe open for writing for
the whole run, so QEMU's open does not wait for a writer and its reads never
meet the end of the pipe.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("serial")

PORTS = ("uart", "usb")


class SerialInput:
    """The run's two input pipes, written without blocking."""

    def __init__(self, outdir: Path):
        self.paths: dict[str, Path] = {}
        self._fds: dict[str, int] = {}
        # Ports that are a model in this process rather than a pipe QEMU reads:
        # the USB OTG controller's CDC port (ADR 0166).
        self._routes: dict[str, object] = {}
        if not hasattr(os, "mkfifo"):
            return                  # a host where no run starts (ADR 0140)
        for port in PORTS:
            path = Path(outdir) / f"{port}.in"
            path.unlink(missing_ok=True)
            os.mkfifo(path)
            # Read and write: opening a pipe this way returns at once, and the
            # pipe never reaches end-of-file while this end stays open.
            self._fds[port] = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self.paths[port] = path

    def write(self, port: str, text: str) -> int:
        """Send `text` to a port. Returns the bytes taken; a full pipe drops
        the rest, which is a serial line's own way of losing input."""
        return self.write_bytes(port, text.encode("utf-8"))

    def route(self, port: str, take) -> None:
        """Send what is typed to `port` to `take(data)` instead of a pipe."""
        self._routes[port] = take

    def write_bytes(self, port: str, data: bytes) -> int:
        """Send raw bytes to a port, as a host tool does (ADR 0158)."""
        if port in self._routes:
            self._routes[port](bytes(data))
            return len(data)
        fd = self._fds.get(port)
        if fd is None:
            log.warning("no serial port %r to type into", port)
            return 0
        try:
            taken = os.write(fd, data)
        except BlockingIOError:
            taken = 0
        if taken < len(data):
            log.warning("%s input full: %d of %d byte(s) dropped", port,
                        len(data) - taken, len(data))
        return taken

    def close(self) -> None:
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()
        for path in self.paths.values():
            path.unlink(missing_ok=True)


class SerialBridge:
    """A serial port on a TCP port, for the host's own tools (ADR 0158).

    One client at a time. What it sends goes into the port's input pipe; what
    the guest sends from the moment it connected goes to it, read from the
    port's log as the log grows.
    """

    POLL_S = 0.02

    def __init__(self, port: str, tcp_port: int, log_path: Path, serial: "SerialInput",
                 bind: str = "127.0.0.1"):
        import socket
        import threading
        self.port = port
        self.log_path = Path(log_path)
        self.serial = serial
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((bind, tcp_port))
        self.listener.listen(1)
        self.address = self.listener.getsockname()
        self.clients = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name=f"serial-{port}",
                                        daemon=True)
        self._thread.start()

    def _size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def _serve(self) -> None:
        import select
        self.listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, peer = self.listener.accept()
            except OSError:
                continue
            self.clients += 1
            log.info("serial %s: a client at %s:%d", self.port, *peer[:2])
            offset = self._size()
            conn.setblocking(False)
            try:
                while not self._stop.is_set():
                    ready, _w, _x = select.select([conn], [], [], self.POLL_S)
                    if ready:
                        data = conn.recv(4096)
                        if not data:
                            break
                        self.serial.write_bytes(self.port, data)
                    size = self._size()
                    if size > offset:
                        with self.log_path.open("rb") as fh:
                            fh.seek(offset)
                            chunk = fh.read(size - offset)
                        offset += len(chunk)
                        conn.setblocking(True)
                        conn.sendall(chunk)
                        conn.setblocking(False)
            except OSError:
                pass
            finally:
                conn.close()
                log.info("serial %s: the client left", self.port)

    def close(self) -> None:
        self._stop.set()
        self.listener.close()
        self._thread.join(timeout=2)
