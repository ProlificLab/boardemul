#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The T-Deck screen's backlight, as the firmware drives its pin (ADR 0151).

GPIO 42 is the backlight's pin on the board (`BOARD_BL_PIN`, LilyGO's
`utilities.h`). The GPIO block tells the board each edge on it and which
signal the matrix routes to it, with the guest time (ADR 0151). Two ways of
driving it are met here:

- **An LED controller channel**, as Meshtastic routes one: the brightness is
  that channel's duty over its full scale (ADR 0053).
- **The pin's own output**, as LilyGO's UnitTest `setBrightness` drives it:
  sixteen steps, the brightest when the pin goes high from off, one step
  down for each short low pulse, the lowest wrapping round to the
  brightest, and off when held low. Short and long are told apart at 1 ms,
  between the two lengths LilyGO's code uses: microseconds for a step, 3 ms
  for off.
"""

from __future__ import annotations

import socket
import struct
from pathlib import Path

STEPS = 16
OFF_NS = 1_000_000
SIG_GPIO_OUT = 256          # SIG_GPIO_OUT_IDX, gpio_sig_map.h
LEDC_FIRST = 73             # LEDC_LS_SIG_OUT0_IDX
LEDC_CHANNELS = 8
REPORT = struct.Struct("<BBHIQ")    # pin, level, out_sel, reserved, guest ns


class GpioReports:
    """The GPIO block's reports on the pins a board asked about (ADR 0151):
    a stream it connects to before the guest runs, read without blocking. A
    stream rather than datagrams, which Linux queues ten of by default: one
    change of brightness is thirty edges inside a few microseconds."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: socket.socket | None = None
        self.pending = b""
        self.listener: socket.socket | None = None
        if not hasattr(socket, "AF_UNIX"):
            # A host where no run starts natively (ADR 0140): the board is
            # built, as fast tests build it, and hears nothing.
            return
        self.path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.listener.setblocking(False)

    def poll(self) -> list[tuple[int, int, int, int]]:
        """Every report waiting: (pin, level, out_sel, guest ns)."""
        if self.listener is None:
            return []
        if self.conn is None:
            try:
                self.conn, _ = self.listener.accept()
                self.conn.setblocking(False)
            except (BlockingIOError, InterruptedError, OSError):
                return []
        while True:
            try:
                data = self.conn.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if not data:
                break
            self.pending += data
        whole = len(self.pending) - len(self.pending) % REPORT.size
        found = []
        for at in range(0, whole, REPORT.size):
            pin, level, out_sel, _reserved, ns = REPORT.unpack_from(self.pending, at)
            found.append((pin, level, out_sel, ns))
        self.pending = self.pending[whole:]
        return found

    def close(self) -> None:
        for sock in (self.conn, self.listener):
            if sock is not None:
                sock.close()
        self.path.unlink(missing_ok=True)


class Backlight:
    """The screen's brightness from what drives its pin."""

    def __init__(self, ledc=None):
        self.ledc = ledc
        self.out_sel: int | None = None    # unknown until the matrix says
        self.high = False
        self.step = 0                      # 0 is off; 16 the brightest
        self.low_since: int | None = None
        self.edges = 0

    def report(self, level: int, out_sel: int, ns: int) -> None:
        self.out_sel = out_sel
        if out_sel != SIG_GPIO_OUT:
            return
        if level and not self.high:
            self.edges += 1
            was_off = (self.step == 0 or self.low_since is None
                       or ns - self.low_since >= OFF_NS)
            # From off the driver lights at its brightest; a short low pulse
            # is one step down, the lowest wrapping to the brightest.
            self.step = STEPS if was_off else (self.step - 1 or STEPS)
            self.high = True
        elif not level and self.high:
            self.high = False
            self.low_since = ns

    def brightness(self, now_ns: int) -> float | None:
        """0 to 1, or None when nothing known drives the pin."""
        if self.out_sel is not None and \
                LEDC_FIRST <= self.out_sel < LEDC_FIRST + LEDC_CHANNELS:
            if self.ledc is None:
                return None
            duty = self.ledc.brightness(self.out_sel - LEDC_FIRST)
            return 0.0 if duty is None else duty
        if self.out_sel != SIG_GPIO_OUT:
            return None
        if self.high:
            return self.step / STEPS
        if self.low_since is None or now_ns - self.low_since >= OFF_NS:
            return 0.0
        return self.step / STEPS             # a step's pulse, mid-way
