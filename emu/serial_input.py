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
        fd = self._fds.get(port)
        if fd is None:
            log.warning("no serial port %r to type into", port)
            return 0
        data = text.encode("utf-8")
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
