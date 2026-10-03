#!/usr/bin/env python3
"""Host-to-guest input events, delivered as a tailed file (ADR 0010).

The emulator runs in a container and the viewer runs on the host, so the two
cannot share a socket without giving the bridge a TCP transport. They already
share a filesystem mount, and the viewer already watches a file for frames --
this is the same trick pointed the other way.

Events are JSON, one per line, appended by whoever is producing them:

    {"type": "key",   "key": "Q", "down": true}
    {"type": "text",  "text": "emu1"}
    {"type": "touch", "x": 120, "y": 200, "down": true}
    {"type": "trackball", "roll": "up", "steps": 6}
    {"type": "trackball", "press": true}

A key event names a key and says whether it went down or up. A text event
is characters, which a board types as the keys its own keyboard needs for
them (ADR 0127).

Anything unparseable is skipped rather than raised: a half-written line is a
normal consequence of tailing a file someone else is appending to.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

log = logging.getLogger("input")

POLL_INTERVAL = 0.03


class InputTail:
    """Tails a JSON-lines file into a queue on its own thread.

    The queue is the only object shared with the bus thread, which keeps every
    device model single-threaded and free of locking.
    """

    def __init__(self, path: str | Path, interval: float = POLL_INTERVAL):
        self.path = Path(path)
        self.interval = interval
        self.queue: queue.Queue[dict] = queue.Queue()
        self._offset = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.delivered = 0
        self.malformed = 0

    def start(self) -> None:
        # Start from the end of any file left over from a previous run, so a
        # stale event log does not replay itself into a fresh boot.
        if self.path.exists():
            self._offset = self.path.stat().st_size
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")
        self._thread = threading.Thread(target=self._run, name="input", daemon=True)
        self._thread.start()
        log.info("tailing %s", self.path)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_new()
            except OSError:
                pass        # file may vanish between runs; try again next tick
            time.sleep(self.interval)

    def _read_new(self) -> None:
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size < self._offset:
            self._offset = 0        # truncated, most likely a new run
        if size == self._offset:
            return
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
            # Only consume up to the last newline: a trailing partial line is
            # someone still writing, and will be complete next tick.
            cut = chunk.rfind("\n")
            if cut < 0:
                return
            self._offset += len(chunk[:cut + 1].encode("utf-8"))
            for line in chunk[:cut].splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self.queue.put(json.loads(line))
                    self.delivered += 1
                except json.JSONDecodeError:
                    self.malformed += 1

    def drain(self) -> list[dict]:
        # Called on every bridge access, so the empty case has to be free.
        # Without this guard each call builds and catches an Empty exception,
        # which measurably slows the whole emulator down.
        if self.queue.empty():
            return []
        events = []
        while True:
            try:
                events.append(self.queue.get_nowait())
            except queue.Empty:
                return events


def append(path: str | Path, event: dict) -> None:
    """Write one event. Used by producers -- the viewer, or a test script."""
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        fh.flush()
