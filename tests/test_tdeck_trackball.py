#!/usr/bin/env python3
"""The T-Deck's trackball against LilyGO's test and Meshtastic (ADR 0128).

Each step of roll toggles its line, as LilyGO's mouse_read() counts them;
changes are paced so each is its own edge; a click holds the boot pin low.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.tdeck_trackball import (  # noqa: E402
    MIN_PRESS_S, NOTCH, PINS, STEP_S, Trackball,
)


class Lines:
    """What the GPIO block would be driven to, change by change."""

    def __init__(self) -> None:
        self.changes: list[tuple[float, int, int]] = []
        self.now = 0.0

    def drive(self, pin: int, level: int) -> None:
        self.changes.append((self.now, pin, level))

    def run(self, ball: Trackball, seconds: float, tick: float = 0.002) -> None:
        end = self.now + seconds
        while self.now < end:
            ball.tick(self.now)
            self.now += tick

    def on(self, pin: int) -> list[int]:
        return [level for _t, p, level in self.changes if p == pin]


def falling(levels: list[int], start: int = 1) -> int:
    count, last = 0, start
    for level in levels:
        count += last == 1 and level == 0
        last = level
    return count


def test_the_pins_are_lilygos() -> None:
    assert PINS == {"press": 0, "left": 1, "right": 2, "up": 3, "down": 15}


def test_each_step_toggles_its_line() -> None:
    lines = Lines()
    ball = Trackball(lines.drive)
    ball.roll("up", 4)
    lines.run(ball, 0.1)
    assert lines.on(PINS["up"]) == [0, 1, 0, 1]
    assert ball.steps["up"] == 4


def test_a_notch_is_three_falling_edges() -> None:
    """Meshtastic's T-Deck threshold, from a line at rest."""
    lines = Lines()
    ball = Trackball(lines.drive)
    ball.roll("down")
    lines.run(ball, 0.1)
    assert len(lines.on(PINS["down"])) == NOTCH
    assert falling(lines.on(PINS["down"])) == 3


def test_changes_are_paced_so_none_merge() -> None:
    lines = Lines()
    ball = Trackball(lines.drive)
    ball.roll("left", 5)
    ball.roll("right", 5)
    lines.run(ball, 0.2)
    times = [t for t, _p, _l in lines.changes]
    assert len(times) == 10
    assert all(b - a >= STEP_S - 1e-9 for a, b in zip(times, times[1:]))
    # In the order asked for.
    assert [p for _t, p, _l in lines.changes] == [PINS["left"]] * 5 + [PINS["right"]] * 5


def test_an_unknown_roll_is_refused() -> None:
    ball = Trackball(lambda pin, level: None)
    assert not ball.roll("sideways")
    assert not ball.roll("up", 0)


def test_a_click_holds_the_boot_pin_low_while_held() -> None:
    lines = Lines()
    ball = Trackball(lines.drive)
    ball.press(True, lines.now)
    lines.run(ball, 1.0)
    assert lines.on(PINS["press"]) == [0], "a long press stays low"
    ball.press(False, lines.now)
    lines.run(ball, 0.01)
    assert lines.on(PINS["press"]) == [0, 1]


def test_a_quick_click_is_held_long_enough_to_be_seen() -> None:
    lines = Lines()
    ball = Trackball(lines.drive)
    ball.press(True, 0.0)
    ball.press(False, 0.01)
    lines.run(ball, 0.5)
    (down, _p, _l), (up, _p2, _l2) = [c for c in lines.changes if c[1] == PINS["press"]]
    assert up - down >= MIN_PRESS_S - 0.002
