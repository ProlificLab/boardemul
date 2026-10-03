#!/usr/bin/env python3
"""The T-Deck's trackball: four direction lines and a click (ADR 0128).

LilyGO's own test (examples/UnitTest, mouse_read) counts every change of
level on a direction line as one step, so each step of roll toggles its line;
the click is the boot pin, low while held. All five lines are pulled up, and
rest high (ADR 0026).

The host asks for rolls faster than a guest can take them, and a line's
interrupt latches one edge until it is served, so changes are paced: one per
STEP_S of host time, each its own interrupt. A click is held for at least
MIN_PRESS_S, so a quick one still spans the firmware's poll.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Callable

log = logging.getLogger("tdeck.trackball")

# [UnitTest utilities.h] BOARD_TBOX_G01..G04 and BOARD_BOOT_PIN, by the way
# mouse_read() moves its cursor for each.
PINS = {"press": 0, "left": 1, "right": 2, "up": 3, "down": 15}
DIRECTIONS = ("up", "down", "left", "right")

STEP_S = 0.004          # one line change per 4 ms of host time
MIN_PRESS_S = 0.2       # the shortest click the firmware is handed
NOTCH = 6               # the window's unit of roll: three cycles of a line


class Trackball:
    """The five lines' levels, driven through `drive(pin, level)`."""

    def __init__(self, drive: Callable[[int, int], None]):
        self.drive = drive
        self.levels = {name: 1 for name in PINS}
        self.queue: deque[str] = deque()
        self.pressed = False            # what the host says
        self._release_at: float | None = None
        self._pressed_at = 0.0
        self._next_step = 0.0
        self.steps = {name: 0 for name in DIRECTIONS}

    def roll(self, direction: str, steps: int = NOTCH) -> bool:
        """Queue `steps` changes of one direction's line."""
        if direction not in DIRECTIONS or steps <= 0:
            return False
        self.queue.extend([direction] * steps)
        return True

    def press(self, down: bool, now: float) -> None:
        if down and not self.pressed:
            self.pressed = True
            self._release_at = None
            self._pressed_at = now
            self._set("press", 0)
        elif not down and self.pressed:
            self.pressed = False
            # Held at least MIN_PRESS_S from the press, then let go.
            self._release_at = max(now, self._pressed_at + MIN_PRESS_S)

    def tick(self, now: float) -> None:
        """Called by the board's input ticker: release a click that is due,
        and change at most one direction line."""
        if self._release_at is not None and now >= self._release_at:
            self._release_at = None
            self._set("press", 1)
        if self.queue and now >= self._next_step:
            direction = self.queue.popleft()
            self.steps[direction] += 1
            self._set(direction, 1 - self.levels[direction])
            self._next_step = now + STEP_S

    def _set(self, name: str, level: int) -> None:
        self.levels[name] = level
        self.drive(PINS[name], level)
