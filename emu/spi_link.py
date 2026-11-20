#!/usr/bin/env python3
"""Receive completed SPI transactions from the in-QEMU controller (ADR 0011).

The controller batches on chip-select and only sends a segment when a
transaction ends or the data/command line changes, so what arrives here is a
command followed by its payload -- not a stream of single bytes.

Segment header, matching esp_spi_master.c:

    u8 type   0 = write only, no reply    1 = duplex, reply of `len` expected
              2 = the chip select went high
              3 = the same, for a part with a BUSY line: answer BUSY below
              4 = the part's reset pin moved, `dc` its level: answer BUSY
              5 = skip: the host clocked `len` bytes of 0xFF in duplex and
                  took the answers the part offered for them (ADR 0172)
    u8 dc     level of the data/command pin while these bytes were clocked
    u8 cs     which device on the bus was selected (ADR 0032)
    u8 pad
    u32 len
    u64 at    the guest's time when the segment was sent, in ns (ADR 0112)

A duplex segment is answered with its `len` bytes, then an offer (ADR 0172):
a u16 length and that many bytes, what the part will answer to the next
bytes if they are all 0xFF on the same select and data/command level.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from pathlib import Path

log = logging.getLogger("spi")

SEGMENT = struct.Struct("<BBBBIQ")
SEG_WRITE = 0
SEG_DUPLEX = 1
SEG_RELEASE = 2         # the chip select went high; the window is over
SEG_RELEASE_BUSY = 3    # the same, and the part has a BUSY line (ADR 0111)
SEG_RESET = 4           # the part's reset pin moved; dc is its new level
SEG_SKIP = 5            # 0xFF bytes answered from the part's offer (ADR 0172)

# An offer's length, and the most a part may offer: esp_spi_master.c's buffer.
# BOARDEMUL_SPI_OFFERS=0 makes every offer empty, every byte an exchange
# again, for telling a fault of the offers from one they only moved in time.
OFFER = struct.Struct("<H")
OFFER_MAX = 2048 if os.environ.get("BOARDEMUL_SPI_OFFERS", "1") != "0" else 0

# The answer to a release or a reset (ADR 0111, ADR 0112): how long the part's
# BUSY line stays high, in guest nanoseconds -- from now, and when that is
# BUSY_HELD, from the next time its select falls -- then its interrupt line:
# its level now, or LINE_LEFT, and the guest time it next rises, or 0.
ANSWER = struct.Struct("<IIB3xQ")
BUSY_HELD = 0xFFFFFFFF
LINE_LEFT = 0xFF


class SpiDevice:
    """A device on one chip-select. `dc` is 0 for command, 1 for data.

    `now_ns` is the guest's time when the segment, release or reset being
    handled was sent, set by the server before each call (ADR 0112)."""

    name = "spi-device"
    now_ns = 0

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        return None

    def ahead(self, limit: int) -> bytes:
        """What this part will answer to the next `limit` bytes if they are
        all 0xFF, clocked in duplex with nothing else in between -- without
        answering them (ADR 0172). Up to `limit` bytes; fewer, or none, where
        the part cannot say. The controller answers those bytes from this and
        later replays them through on_segment, so a part may offer only what
        does not depend on when the bytes come."""
        return b""

    def on_deselect(self) -> None:
        """The chip select went high, so whatever was being clocked is over.

        A device with fixed-length exchanges can ignore this. One whose command
        length is decided by the host cannot: it is the only thing that says a
        command has ended.
        """

    def on_reset(self, asserted: bool) -> None:
        """The part's reset pin moved: `asserted` while it is held low. Only a
        select the controller was given a reset pin for hears this."""

    def busy(self) -> tuple[int, int]:
        """How long this part's BUSY line stays high now that a window has
        closed or its reset pin has moved: `(high_ns, wake_ns)` on the guest's
        clock. `high_ns` of BUSY_HELD keeps it high until the select next
        falls, and it falls `wake_ns` after that edge. Asked only for a select
        the controller was given a BUSY pin for (ADR 0111). A part that is
        never busy says so."""
        return (0, 0)

    def line(self) -> tuple[int | None, int]:
        """The part's interrupt line, asked at the same moments as busy():
        its level now, or None to leave it, and the guest time in ns it next
        rises, or 0. Asked only for a select the controller was given an
        interrupt pin for (ADR 0112)."""
        return (None, 0)

    def flush(self) -> None:
        """Called once the run is over. A panel with nothing pending does
        nothing; one caught part-way through an update emits what it has."""


class SpiServer:
    """Listens for the controller and services it on its own thread.

    One connection, one bus. The controller says which chip select was
    asserted for each segment (ADR 0032) and the segment goes to the device on
    that select. A board with one device passes it alone and it sits on select
    zero, which is what a controller told about no chip select reports.
    """

    def __init__(self, socket_path: str | Path,
                 device: SpiDevice | dict[int, SpiDevice]):
        self.socket_path = str(socket_path)
        self.devices: dict[int, SpiDevice] = (
            dict(device) if isinstance(device, dict) else {0: device})
        # Chip selects the controller clocked bytes into with nothing on
        # them. A part the firmware talks to and this board does not model
        # is the whole question of what hardware is missing, so it is
        # counted rather than only noticed (ADR 0053).
        self.unmodelled: dict[int, int] = {}
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.segments = 0
        self.bytes_in = 0

    @property
    def device(self) -> SpiDevice | None:
        """The device on chip select zero, which on a one-device board is the
        only one there is. Kept because most callers and tests mean that."""
        return self.devices.get(0)

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
            kind, dc, cs, _pad, length, at_ns = SEGMENT.unpack(header)
            # A skip counts bytes the part already gave; none follow it.
            payload = self._recv(conn, length) if length and kind != SEG_SKIP else b""
            if payload is None:
                return
            self.segments += 1
            self.bytes_in += len(payload)
            device = self.devices.get(cs)
            if device is not None:
                device.now_ns = at_ns
            if kind == SEG_SKIP:
                # Bytes the controller answered from this part's offer: the
                # part takes them now, as it would have one by one.
                if device is not None:
                    try:
                        device.on_segment(dc, b"\xff" * length, True)
                    except Exception:
                        log.exception("spi device raised replaying %d offered bytes",
                                      length)
                continue
            if kind in (SEG_RELEASE, SEG_RELEASE_BUSY, SEG_RESET):
                if device is not None:
                    try:
                        if kind == SEG_RESET:
                            device.on_reset(asserted=not dc)
                        else:
                            device.on_deselect()
                    except Exception:
                        log.exception("spi device raised on %s",
                                      "reset" if kind == SEG_RESET else "deselect")
                if kind != SEG_RELEASE and not self._answer_busy(conn, device):
                    return
                continue
            if device is None:
                # A chip select with nothing on it. Real hardware clocks the
                # bytes into empty air, so drop them and answer a duplex read
                # with the idle level of an undriven line.
                if kind == SEG_DUPLEX:
                    self.unmodelled[cs] = self.unmodelled.get(cs, 0) + 1
                reply = None
            else:
                try:
                    reply = device.on_segment(dc, payload, kind == SEG_DUPLEX)
                except Exception:
                    # A model bug must not wedge the guest mid-transaction.
                    log.exception("spi device raised on a %d byte segment", length)
                    reply = None
            if kind == SEG_DUPLEX:
                out = (reply or b"")[:length].ljust(length, b"\x00")
                offer = b""
                if device is not None:
                    try:
                        offer = device.ahead(OFFER_MAX)[:OFFER_MAX]
                    except Exception:
                        log.exception("spi device raised making an offer")
                try:
                    conn.sendall(out + OFFER.pack(len(offer)) + offer)
                except OSError:
                    return

    @staticmethod
    def _answer_busy(conn: socket.socket, device: SpiDevice | None) -> bool:
        """The controller is waiting to hear about BUSY and the interrupt
        line, so it always gets an answer: from a part that raises, or from no
        part, that is "not busy, line left alone", which is what both lines
        were before they were modelled."""
        high_ns, wake_ns, level, rise_at = 0, 0, None, 0
        if device is not None:
            try:
                high_ns, wake_ns = device.busy()
                level, rise_at = device.line()
            except Exception:
                log.exception("spi device raised answering for its lines")
                high_ns, wake_ns, level, rise_at = 0, 0, None, 0
        try:
            conn.sendall(ANSWER.pack(min(high_ns, BUSY_HELD), min(wake_ns, BUSY_HELD),
                                     LINE_LEFT if level is None else int(bool(level)),
                                     max(0, rise_at)))
        except OSError:
            return False
        return True

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
