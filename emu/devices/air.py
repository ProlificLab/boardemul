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

Boards that keep one time (ADR 0113) also meet here: each says so in its file,
and at every barrier sends the others a mark with its guest time, then waits
for theirs. A mark goes out on the socket the board's frames go out on, after
them, and loopback keeps one socket's datagrams in order -- so when a peer's
mark is in hand, so is every frame it sent before it.
"""

from __future__ import annotations

import logging
import os
import select
import socket
import struct
import time
from pathlib import Path

log = logging.getLogger("air")

SUFFIX = ".air"
MAX_FRAME = 512
HOST = "127.0.0.1"
# A board's file says its port, and "timed" if it keeps time with the others.
TIMED = "timed"
# A barrier mark: who has reached which guest time, in nanoseconds.
MARK = struct.Struct("<4sQ")
MARK_MAGIC = b"MARK"
# How long a barrier waits between looks at who is still on the air. A peer
# whose file has gone has left and is not waited for.
RELIST_S = 0.2
# How often a timed board says how its barriers are going.
REPORT_EVERY = 1000


class AirMedium:
    """One board's connection to the air."""

    def __init__(self, directory: str | Path, name: str | None = None,
                 timed: bool = False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.name = name or f"{os.getpid()}"
        self.timed = timed
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.path = self.directory / f"{self.name}{SUFFIX}"
        self.path.write_text(f"{self.port} {TIMED}" if timed else str(self.port))
        self.sent = 0
        self.received = 0
        # The latest mark each timed peer has sent, by port (ADR 0113).
        self.marks: dict[int, int] = {}
        self.barriers = 0
        self.waited_s = 0.0
        self._peers: dict[int, str] = {}
        self._peers_at: float | None = None
        self._reported: tuple[float, float] | None = None
        log.info("on the air as %s on port %d%s", self.path.name, self.port,
                 ", keeping time with the others" if timed else "")

    def _listing(self) -> list[tuple[str, int, bool]]:
        """Everyone else present: name, port, and whether they keep time. Read
        each time: boards come and go, and a file left behind is a board that
        has stopped."""
        found = []
        for entry in sorted(self.directory.glob("*" + SUFFIX)):
            if entry == self.path:
                continue
            try:
                words = entry.read_text().split()
                found.append((entry.name[:-len(SUFFIX)], int(words[0]),
                              TIMED in words[1:]))
            except (OSError, ValueError, IndexError):
                continue        # being written, or left behind by a dead board
        return found

    def peers(self) -> list[int]:
        """Everyone else present, by port."""
        return [port for _name, port, _timed in self._listing()]

    def timed_peers(self) -> dict[int, str]:
        """The others keeping time on this air, by port, with their names."""
        return {port: name for name, port, timed in self._listing() if timed}

    def _timed_recently(self, fresh: bool = False) -> dict[int, str]:
        """timed_peers(), read again at most every RELIST_S: a barrier comes
        every quantum, and the directory changes when a board starts or stops."""
        now = time.monotonic()
        if fresh or self._peers_at is None or now - self._peers_at > RELIST_S:
            self._peers = self.timed_peers()
            self._peers_at = now
        return self._peers

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
        return [frame for _port, frame in self._drain()]

    def poll_named(self) -> list[tuple[str, bytes]]:
        """Whatever has arrived, each with the board that sent it: the port it
        came from, as that board's file in the directory names it. Who sent a
        frame is the air's to say, not the packet's: a relayed packet carries
        another node's address (ADR 0141)."""
        names = {port: name for name, port, _timed in self._listing()}
        return [(names.get(port, f"port {port}"), frame) for port, frame in self._drain()]

    def _drain(self) -> list[tuple[int, bytes]]:
        """Every datagram waiting, with the port it came from. Marks are
        taken out and kept; frames are returned."""
        frames = []
        while True:
            try:
                data, (_host, port) = self.sock.recvfrom(MAX_FRAME)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if len(data) == MARK.size and data[:4] == MARK_MAGIC:
                _magic, ns = MARK.unpack(data)
                self.marks[port] = max(ns, self.marks.get(port, -1))
                continue
            frames.append((port, data))
        self.received += len(frames)
        return frames

    def barrier(self, ns: int, boards: int, give_up_s: float | None = None
                ) -> list[tuple[str, bytes]]:
        """Reach guest time `ns` together with every other timed board.

        At the first barrier, wait until `boards` boards keeping time are on
        the air, counting this one, so that nobody's time runs ahead of a board
        still starting. Then mark `ns` to every timed peer and wait for a mark
        at least as late from each. Returns the frames that arrived meanwhile,
        with the name of the board that sent each.
        """
        started = time.monotonic()
        peers = self._timed_recently(fresh=self.barriers == 0)
        while self.barriers == 0 and len(peers) + 1 < boards:
            if give_up_s is not None and time.monotonic() - started > give_up_s:
                break
            time.sleep(0.05)
            peers = self._timed_recently(fresh=True)
        mark = MARK.pack(MARK_MAGIC, ns)
        for port in peers:
            try:
                self.sock.sendto(mark, (HOST, port))
            except OSError:
                continue
        got: list[tuple[str, bytes]] = []
        while True:
            got += [(peers.get(port, str(port)), frame)
                    for port, frame in self._drain()]
            waiting = [port for port in peers if self.marks.get(port, -1) < ns]
            if not waiting:
                break
            if give_up_s is not None and time.monotonic() - started > give_up_s:
                log.warning("barrier at %d ns gave up on %s", ns,
                            [peers[port] for port in waiting])
                break
            select.select([self.sock], [], [], 0.05)
            # A peer whose file has gone has left, and is not waited for.
            present = self._timed_recently()
            peers = {port: name for port, name in peers.items() if port in present}
        self.barriers += 1
        self.waited_s += time.monotonic() - started
        if self.barriers % REPORT_EVERY == 0:
            now = time.monotonic()
            if self._reported is not None:
                span = now - self._reported[0]
                log.info("barrier %d at guest %.3f s: %.2f ms of wall time each, "
                         "%.2f ms of it waiting for the others", self.barriers,
                         ns / 1e9, 1000 * span / REPORT_EVERY,
                         1000 * (self.waited_s - self._reported[1]) / REPORT_EVERY)
            self._reported = (now, self.waited_s)
        return got

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            if self.path.exists():
                self.path.unlink()

    def __repr__(self) -> str:
        return (f"<air {self.path.name} port={self.port} sent={self.sent} "
                f"received={self.received} peers={len(self.peers())}>")


def keep_time(radio, medium: AirMedium, gpio, line_pin: int, boards: int):
    """What a board's guest clock does at each barrier on a timed air (ADR
    0113): meet the other boards, let the radio judge what they sent, and put
    its interrupt line where that leaves it -- all at the same guest instant.

    The first barrier also carries every level the host has set on the
    board's input pins. Sent over the GPIO socket alone, they land whenever
    QEMU's main loop next takes the lock, which under load can be after the
    guest's first instructions; at the barrier they are in place at time zero,
    before any.
    """
    first = [True]

    def on_barrier(ns: int) -> list:
        lines = [(line_pin, *radio.barrier(ns, medium.barrier(ns, boards)))]
        if first[0]:
            first[0] = False
            lines = [(pin, level, 0) for pin, level in sorted(gpio.levels.items())
                     if pin != line_pin] + lines
        return lines
    return on_barrier
