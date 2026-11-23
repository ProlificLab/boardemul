#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The LED controller, which is what drives the screen's backlight.

This window was claimed but not modelled, and of everything still claimed it is
by far the most used: across every run this project has kept, thirty-five
thousand accesses, second only to the WiFi front end. What the firmware does
with it is set the panel's brightness -- including fading it out before it
blanks the screen, which the emulator could not show at all while the writes
landed in a window that only remembered them.

The layout is the chip's, and the values below were read off a run rather than
taken from a table:

    CHn_CONF0   +0x00 + n*0x14   timer in bits 1..0, output enable in bit 2
    CHn_HPOINT  +0x04 + n*0x14
    CHn_DUTY    +0x08 + n*0x14   the duty, shifted up by four
    CHn_CONF1   +0x0c + n*0x14   bit 31 starts it, bit 30 says which way
    CHn_DUTY_R  +0x10 + n*0x14   what the channel is actually outputting
    TIMERx_CONF +0xa0 + x*8      resolution in bits 4..0, divider in 22..5

Brightness is the duty over its full scale, and the full scale is the timer's
resolution: the firmware seen here uses nine bits on timer three, ramps to 307
of 511, and later steps back down to 2, which is the screen going dark.

Writing `CHn_CONF1` with bit 31 is what latches a new duty. The hardware clears
that bit once it has, and the driver reads the register back, so this clears it
too -- a bit that stays set is the kind of thing a driver spins on.
"""

from __future__ import annotations

import logging

from emu.bus import Region

log = logging.getLogger("ledc")

CHANNELS = 8
TIMERS = 4
CHANNEL_STRIDE = 0x14
TIMER_BASE = 0xA0
TIMER_STRIDE = 0x08
# The duty register carries four fractional bits below the whole duty.
DUTY_SHIFT = 4
CONF0_TIMER = 0x3
CONF0_OUTPUT_ENABLE = 1 << 2
CONF1_START = 1 << 31
CONF1_INCREASING = 1 << 30
TIMER_RESOLUTION = 0x1F
TIMER_DIVIDER_SHIFT = 5
TIMER_DIVIDER = 0x3FFFF


class Channel:
    def __init__(self, index: int):
        self.index = index
        self.conf0 = 0
        self.conf1 = 0
        self.duty_register = 0     # what was written, awaiting a start
        self.duty = 0              # what the channel is putting out
        self.hpoint = 0
        self.changes = 0

    @property
    def timer(self) -> int:
        return self.conf0 & CONF0_TIMER

    @property
    def enabled(self) -> bool:
        return bool(self.conf0 & CONF0_OUTPUT_ENABLE)


class Timer:
    def __init__(self, index: int):
        self.index = index
        self.conf = 0

    @property
    def resolution(self) -> int:
        return self.conf & TIMER_RESOLUTION

    @property
    def full_scale(self) -> int:
        """The largest duty the timer can count to. A resolution of zero means
        the timer has not been set up, and nothing can be a fraction of it."""
        bits = self.resolution
        return (1 << bits) - 1 if bits else 0

    @property
    def divider(self) -> int:
        return (self.conf >> TIMER_DIVIDER_SHIFT) & TIMER_DIVIDER


class Ledc(Region):
    """Reports what each channel is driving, as a fraction of its full scale.

    Which pin a channel reaches is decided in the GPIO matrix, which lives
    inside the machine's own GPIO block and is not visible from here, so this
    model reports channels and does not claim to know which one is the
    backlight (ADR 0053).
    """

    def __init__(self, name: str = "ledc", base: int = 0x60019000,
                 size: int = 0x1000):
        super().__init__(name, base, size)
        self.channels = [Channel(i) for i in range(CHANNELS)]
        self.timers = [Timer(i) for i in range(TIMERS)]
        self.regs: dict[int, int] = {}
        # Every level each channel has settled at, in order, so a run can be
        # asked afterwards whether the firmware dimmed the screen.
        self.levels: dict[int, list[float]] = {i: [] for i in range(CHANNELS)}

    # -- what the guest sees ---------------------------------------------

    def read(self, offset: int, size: int) -> int:
        channel, register = self._channel_at(offset)
        if channel is not None:
            if register == 0x00:
                return channel.conf0
            if register == 0x04:
                return channel.hpoint
            if register == 0x08:
                return channel.duty_register
            if register == 0x0C:
                return channel.conf1
            if register == 0x10:
                # What it is actually outputting, which is only the same as the
                # duty register once a start has latched it.
                return channel.duty << DUTY_SHIFT
        timer = self._timer_at(offset)
        if timer is not None and (offset - TIMER_BASE) % TIMER_STRIDE == 0:
            return timer.conf
        return self.regs.get(offset, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        self.regs[offset] = value
        channel, register = self._channel_at(offset)
        if channel is not None:
            if register == 0x00:
                channel.conf0 = value
            elif register == 0x04:
                channel.hpoint = value
            elif register == 0x08:
                channel.duty_register = value
            elif register == 0x0C:
                # Bit 31 is the hardware being told to take the duty that was
                # written; it clears itself once it has, so this does too.
                channel.conf1 = value & ~CONF1_START
                if value & CONF1_START:
                    self._latch(channel)
            return
        timer = self._timer_at(offset)
        if timer is not None and (offset - TIMER_BASE) % TIMER_STRIDE == 0:
            timer.conf = value

    def reset(self) -> None:
        self.__init__(self.name, self.base, self.size)

    # -- what the board can ask it ---------------------------------------

    def brightness(self, channel: int) -> float | None:
        """How bright this channel is driving, from nought to one, or None if
        it is not driving at all."""
        ch = self.channels[channel]
        scale = self.timers[ch.timer].full_scale
        if not ch.enabled or not scale:
            return None
        return min(1.0, ch.duty / scale)

    def driving(self) -> list[int]:
        return [c.index for c in self.channels if self.brightness(c.index) is not None]

    def summary(self) -> str:
        parts = []
        for index in self.driving():
            seen = self.levels[index]
            now = self.brightness(index)
            parts.append(f"channel {index} at {now:.0%}"
                         + (f", between {min(seen):.0%} and {max(seen):.0%} over "
                            f"{len(seen)} change(s)" if seen else ""))
        return "ledc: " + ("; ".join(parts) if parts else "nothing driving")

    # -- internals -------------------------------------------------------

    def _latch(self, channel: Channel) -> None:
        channel.duty = channel.duty_register >> DUTY_SHIFT
        channel.changes += 1
        level = self.brightness(channel.index)
        if level is not None:
            self.levels[channel.index].append(level)
            log.debug("channel %d at %.1f%%", channel.index, level * 100)

    def _channel_at(self, offset: int) -> tuple[Channel | None, int]:
        if offset >= CHANNELS * CHANNEL_STRIDE:
            return None, 0
        index, register = divmod(offset, CHANNEL_STRIDE)
        return (self.channels[index], register) if register <= 0x10 else (None, 0)

    def _timer_at(self, offset: int) -> Timer | None:
        if not TIMER_BASE <= offset < TIMER_BASE + TIMERS * TIMER_STRIDE:
            return None
        return self.timers[(offset - TIMER_BASE) // TIMER_STRIDE]
