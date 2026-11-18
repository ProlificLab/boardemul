#!/usr/bin/env python3
"""The T-Deck screen's backlight, from what drives its pin (ADR 0151).

Fast: edges as the GPIO block reports them, with their guest times.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.backlight import (  # noqa: E402
    LEDC_FIRST, OFF_NS, REPORT, SIG_GPIO_OUT, STEPS, Backlight, GpioReports,
)

US = 1_000


def set_brightness(light: Backlight, ns: int, level: int, value: int) -> int:
    """LilyGO's setBrightness (examples/UnitTest/UnitTest.ino), as edges.
    Returns the guest time after it, and the new level."""
    if value == 0:
        light.report(0, SIG_GPIO_OUT, ns)
        return ns + 3_000 * US, 0
    if level == 0:
        light.report(1, SIG_GPIO_OUT, ns)
        ns += 30 * US
        level = STEPS
    pulses = (STEPS + (STEPS - value) - (STEPS - level)) % STEPS
    for _ in range(pulses):
        light.report(0, SIG_GPIO_OUT, ns)
        light.report(1, SIG_GPIO_OUT, ns + 1 * US)
        ns += 2 * US
    return ns, value


def test_the_pin_low_from_the_start_is_dark() -> None:
    light = Backlight()
    light.report(0, SIG_GPIO_OUT, 0)
    assert light.brightness(5 * OFF_NS) == 0.0


@pytest.mark.parametrize("value", [16, 12, 8, 3, 1])
def test_lilygos_steps_land_where_its_code_aims(value: int) -> None:
    light = Backlight()
    ns, level = set_brightness(light, 0, 0, value)
    assert light.brightness(ns) == value / STEPS


def test_going_up_wraps_round_as_lilygos_code_counts() -> None:
    """From 4 to 12 is twelve pulses down, through the wrap."""
    light = Backlight()
    ns, level = set_brightness(light, 0, 0, 4)
    ns, level = set_brightness(light, ns, level, 12)
    assert light.brightness(ns) == 12 / STEPS


def test_held_low_is_off_and_high_again_is_the_brightest() -> None:
    light = Backlight()
    ns, level = set_brightness(light, 0, 0, 5)
    ns, level = set_brightness(light, ns, level, 0)
    assert light.brightness(ns) == 0.0
    ns, level = set_brightness(light, ns, level, 16)
    assert light.brightness(ns) == 1.0


def test_a_step_pulse_in_flight_is_not_off() -> None:
    light = Backlight()
    ns, _ = set_brightness(light, 0, 0, 16)
    light.report(0, SIG_GPIO_OUT, ns)
    assert light.brightness(ns + 1 * US) == 1.0
    assert light.brightness(ns + OFF_NS) == 0.0


class FakeLedc:
    def __init__(self, duties: dict[int, float | None]):
        self.duties = duties

    def brightness(self, channel: int) -> float | None:
        return self.duties.get(channel)


def test_an_led_controller_channel_routed_to_it_sets_the_brightness() -> None:
    """Meshtastic's way (ADR 0053), read off the matrix, not assumed."""
    ledc = FakeLedc({7: 0.6, 2: 0.1})
    light = Backlight(ledc)
    light.report(0, LEDC_FIRST + 7, 10)
    assert light.brightness(20) == 0.6
    ledc.duties[7] = None                   # the channel stopped driving
    assert light.brightness(30) == 0.0


def test_a_signal_it_does_not_know_says_nothing() -> None:
    light = Backlight()
    assert light.brightness(0) is None      # nothing heard yet
    light.report(1, 12, 0)                  # some other peripheral's signal
    assert light.brightness(10) is None


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32",
                    reason="Unix datagram sockets")
def test_reports_arrive_as_the_gpio_block_sends_them(tmp_path: Path) -> None:
    reports = GpioReports(tmp_path / "r.sock")
    try:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sender.connect(str(tmp_path / "r.sock"))
        # Thirty-two edges at once, and one split across two reads: none lost.
        burst = b"".join(REPORT.pack(42, i % 2, SIG_GPIO_OUT, 0, i) for i in range(32))
        sender.sendall(burst + REPORT.pack(42, 1, SIG_GPIO_OUT, 0, 99)[:5])
        import time
        time.sleep(0.05)
        got = reports.poll()
        assert [ns for *_x, ns in got] == list(range(32))
        sender.sendall(REPORT.pack(42, 1, SIG_GPIO_OUT, 0, 99)[5:])
        time.sleep(0.05)
        assert reports.poll() == [(42, 1, SIG_GPIO_OUT, 99)]
        sender.close()
    finally:
        reports.close()
