#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The backlight controller (ADR 0053).

The sequence replayed here is one a real run produced, taken off the bus log
rather than invented: nine-bit resolution on timer three, channel seven
enabled, the duty ramped up and then stepped back down to nearly nothing, which
is the interface fading the screen out before it blanks it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.ledc import CONF1_START, Ledc  # noqa: E402

CHANNEL, TIMER = 7, 3
CONF0 = CHANNEL * 0x14 + 0x00
DUTY = CHANNEL * 0x14 + 0x08
CONF1 = CHANNEL * 0x14 + 0x0C
DUTY_R = CHANNEL * 0x14 + 0x10
TIMER_CONF = 0xA0 + TIMER * 8
# What the run wrote: timer three, nine bits of resolution, on the APB tick.
TIMER_NINE_BITS = 0x02001C69
# Timer three selected, output enabled, parameters to be taken up.
CHANNEL_ON = 0x17


@pytest.fixture
def ledc() -> Ledc:
    led = Ledc()
    led.write(TIMER_CONF, TIMER_NINE_BITS, 4)
    led.write(CONF0, CHANNEL_ON, 4)
    return led


def set_duty(led: Ledc, raw: int) -> None:
    """The two writes the driver makes: the duty, then the bit that takes it."""
    led.write(DUTY, raw, 4)
    led.write(CONF1, CONF1_START, 4)


def test_the_duty_is_a_fraction_of_the_timers_full_scale(ledc) -> None:
    """Nine bits is a full scale of 511, and the register carries the duty
    shifted up by four. 0x1330 is the brightest the run ever set."""
    set_duty(ledc, 0x1330)
    assert ledc.brightness(CHANNEL) == pytest.approx(307 / 511, abs=1e-6)


def test_starting_it_clears_the_bit_that_started_it(ledc) -> None:
    """The hardware clears it once the duty is taken, and the driver reads the
    register back -- a bit left set is the kind of thing a driver spins on."""
    set_duty(ledc, 0x0FF0)
    assert not ledc.read(CONF1, 4) & CONF1_START


def test_the_duty_only_counts_once_it_has_been_taken(ledc) -> None:
    """Writing the duty register alone changes nothing: the channel goes on
    driving what it was until it is told to take the new value."""
    set_duty(ledc, 0x0FF0)
    before = ledc.brightness(CHANNEL)
    ledc.write(DUTY, 0x0020, 4)
    assert ledc.brightness(CHANNEL) == before
    ledc.write(CONF1, CONF1_START, 4)
    assert ledc.brightness(CHANNEL) < before


def test_it_reports_what_it_is_driving_not_what_was_written(ledc) -> None:
    set_duty(ledc, 0x0FF0)
    ledc.write(DUTY, 0x0020, 4)
    assert ledc.read(DUTY_R, 4) == 0x0FF0
    assert ledc.read(DUTY, 4) == 0x0020


def test_a_channel_that_was_never_enabled_drives_nothing() -> None:
    """Eight channels exist; a board uses one. The others are not dark, they
    are not driving, and the difference matters to whoever reads this."""
    led = Ledc()
    led.write(TIMER_CONF, TIMER_NINE_BITS, 4)
    assert led.brightness(0) is None
    assert led.driving() == []


def test_a_timer_that_was_never_set_up_has_no_full_scale() -> None:
    """A resolution of zero is a timer nobody configured, and a duty cannot be
    a fraction of it. Dividing by it is what this window was claimed for."""
    led = Ledc()
    led.write(CONF0, CHANNEL_ON, 4)
    set_duty(led, 0x0FF0)
    assert led.brightness(CHANNEL) is None


def test_the_run_can_be_asked_whether_the_screen_was_dimmed(ledc) -> None:
    """The point of modelling it: the interface fades the panel out before it
    blanks, and every level it settled at is kept so a run can say so."""
    for raw in (0x0FF0, 0x1330, 0x0180, 0x0020):
        set_duty(ledc, raw)
    levels = ledc.levels[CHANNEL]
    assert len(levels) == 4
    assert max(levels) == pytest.approx(307 / 511, abs=1e-6)
    assert levels[-1] < 0.01                      # all but off
    assert "channel 7" in ledc.summary()


def test_a_reset_leaves_it_as_a_fresh_boot_finds_it(ledc) -> None:
    """These are registers inside the chip, so the machine's reset clears
    them; a second boot must not inherit the first one's brightness."""
    set_duty(ledc, 0x1330)
    ledc.reset()
    assert ledc.brightness(CHANNEL) is None
    assert ledc.levels[CHANNEL] == []
