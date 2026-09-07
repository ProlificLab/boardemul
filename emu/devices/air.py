#!/usr/bin/env python3
"""The air between two boards (ADR 0036).

A radio that answers is not a radio that carries anything, and a mesh device
alone in a room is only half the thing. This is the medium: every emulator that
joins writes a small file into one directory saying where it can be reached,
and transmitting means handing the frame to everyone else listed there.

A directory rather than a network group because it needs no addresses agreed in
advance, no configuration and no daemon: joining is creating a file, leaving is
the file going away, and who is on the air is something you can list. The
frames themselves go over the loopback interface, which works the same on the
machine this is developed on and in the container it runs in -- a Unix socket
would not.

There is no propagation here. Everyone in the directory hears everything, at
once and intact. Range, collisions, interference and timing are real things a
real mesh spends its effort on, and none of them are modelled; what is modelled
is that a packet one board sends is a packet another board receives.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

log = logging.getLogger("air")

SUFFIX = ".air"
MAX_FRAME = 512
HOST = "127.0.0.1"


class AirMedium:
    """One board's connection to the air."""

    def __init__(self, directory: str | Path, name: str | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.name = name or f"{os.getpid()}"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.path = self.directory / f"{self.name}{SUFFIX}"
        self.path.write_text(str(self.port))
        self.sent = 0
        self.received = 0
        log.info("on the air as %s on port %d", self.path.name, self.port)

    def peers(self) -> list[int]:
        """Everyone else present, by port. Read each time: boards come and go,
        and a file left behind is a board that has stopped."""
        ports = []
        for entry in sorted(self.directory.glob("*" + SUFFIX)):
            if entry == self.path:
                continue
            try:
                ports.append(int(entry.read_text().strip()))
            except (OSError, ValueError):
                continue        # being written, or left behind by a dead board
        return ports

    def send(self, frame: bytes) -> int:
        """Hand a frame to every other board. Returns how many were told."""
        told = 0
        for port in self.peers():
            try:
                self.sock.sendto(frame[:MAX_FRAME], (HOST, port))
                told += 1
            except OSError:
                continue
        self.sent += 1
        log.debug("sent %d bytes to %d peer(s)", len(frame), told)
        return told

    def poll(self) -> list[bytes]:
        """Whatever has arrived since the last look."""
        frames = []
        while True:
            try:
                frames.append(self.sock.recv(MAX_FRAME))
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
        self.received += len(frames)
        return frames

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            if self.path.exists():
                self.path.unlink()

    def __repr__(self) -> str:
        return (f"<air {self.path.name} port={self.port} sent={self.sent} "
                f"received={self.received} peers={len(self.peers())}>")
