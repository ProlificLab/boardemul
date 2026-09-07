#!/usr/bin/env python3
"""A chip select with nothing on it yet, that says what was clocked at it.

ADR 0006: a device is modelled from what a firmware actually sends, not from a
datasheet read in advance. Before there is a radio or a card model there has to
be a record of what the firmware asks them, and a select that silently swallows
its traffic produces none.

This answers a read with the idle level of the bus and logs the exchange.
"""

from __future__ import annotations

import logging

from emu.spi_link import SpiDevice

log = logging.getLogger("spi")

# What an undriven line reads as. A part that is absent holds MISO at neither
# level, and a bus with a pull-up reads high; the firmwares seen so far treat
# all-ones as "no answer", which is what an empty select should look like.
IDLE_BYTE = 0xFF


class SpiTrace(SpiDevice):
    """Records the traffic aimed at one chip select."""

    def __init__(self, name: str, limit: int = 40, reply: int = IDLE_BYTE):
        self.name = name
        self.limit = limit
        self.reply = reply
        self.segments = 0
        self.bytes_in = 0
        self.first: list[tuple[int, bytes, bool]] = []

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        self.segments += 1
        self.bytes_in += len(data)
        if len(self.first) < self.limit:
            self.first.append((dc, data, duplex))
            log.info("%s %s %s", self.name, "xchg" if duplex else "wr ",
                     data[:32].hex())
        return bytes([self.reply]) * len(data) if duplex else None

    def __repr__(self) -> str:
        return (f"<spi-trace {self.name} segments={self.segments} "
                f"bytes={self.bytes_in}>")
