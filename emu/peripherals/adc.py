#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The SAR ADC's one-shot conversion, which is all `analogRead()` needs.

Found by sampling the CPU: once the vendor firmware's interface was up, its
main task sat spinning at one instruction, `l32i a8, a3, 12 / extui a8,a8,16,1 /
beqz`, on `SENS_SAR_MEAS1_CTRL2` -- pulse bit 17 to start a conversion, wait
for bit 16 to say it is done. Nothing modelled the ADC, so it never was, and
LVGL never got another turn. The read is the battery voltage for the status bar.

Register layout is Espressif's `sens_reg.h` for the ESP32-S3: START_SAR is bit
17, DONE_SAR bit 16 and DATA_SAR the low sixteen bits of the same register.
A real conversion takes microseconds and the driver polls straight after the
start, so finishing on the same write is indistinguishable from the hardware.

The value is a modelling choice and says so: there is no battery. `raw` is the
twelve-bit sample handed back; the default reads as a healthy pack once the
firmware's calibration and the board's divider have had their way with it.
"""

from __future__ import annotations

import logging
import time

from emu.bus import Region

log = logging.getLogger("adc")

R_MEAS1_CTRL2 = 0x0C
START_SAR = 1 << 17
DONE_SAR = 1 << 16
DATA_MASK = 0xFFFF

# The temperature sensor shares the page (SENS_SAR_TSENS_CTRL). The driver sets
# DUMP_OUT and spins on READY, then takes the eight-bit reading.
R_TSENS_CTRL = 0x50
TSENS_DUMP_OUT = 1 << 24
TSENS_READY = 1 << 8
TSENS_OUT_MASK = 0xFF
# A modelling choice, like the battery below: there is no die to be warm.
# 128 converts to about 28 C by the chip's default range.
DEFAULT_TSENS_OUT = 128

# Twelve-bit, at the 11 dB attenuation Arduino selects for a battery pin:
# roughly 1.9 V at the pin, about 3.9 V after the divider on this board.
DEFAULT_RAW = 2200


class SarAdc(Region):
    """`DR_REG_SENS_BASE`: everything stored, and one conversion answered."""

    def __init__(self, name: str, base: int, size: int = 0x1000,
                 raw: int = DEFAULT_RAW):
        super().__init__(name, base, size)
        self.regs: dict[int, int] = {}
        self.raw = raw & 0xFFF
        self.conversions = 0

    def reset(self) -> None:
        """On-chip, so a reset returns it to what a first boot finds. The
        sample it answers with is a property of the board, not of the run, and
        stays."""
        self.regs.clear()
        self.conversions = 0
        self._reads = 0

    def read(self, offset: int, size: int) -> int:
        # Progress meter for a firmware that samples in a loop: every
        # conversion reads this register at least twice, so its read count
        # over wall time says whether a calibration sweep is advancing,
        # slowing, or has stopped -- with a constant sample it cannot converge
        # on anything, only run out of steps or not.
        if offset == R_MEAS1_CTRL2:
            self._reads = getattr(self, "_reads", 0) + 1
            if self._reads == 1:
                self._t0 = time.monotonic()
            if self._reads % 1000 == 0:
                log.info("adc: %d result reads at +%.1fs", self._reads, time.monotonic() - self._t0)
        value = self.regs.get(offset, 0)
        if offset == R_TSENS_CTRL and value & TSENS_DUMP_OUT:
            # A reading is ready as soon as one was asked for. Reported, not
            # stored, for the same reason as DONE above.
            value = (value & ~TSENS_OUT_MASK) | TSENS_READY | DEFAULT_TSENS_OUT
        return value

    def write(self, offset: int, value: int, size: int) -> None:
        if offset == R_MEAS1_CTRL2:
            started = value & START_SAR and not (self.regs.get(offset, 0) & START_SAR)
            if started:
                # Conversion complete, result in place, on the same write. The
                # DONE bit is reported, never stored back into what the driver
                # wrote, so its own read-modify-writes see their own values.
                value = (value & ~DATA_MASK) | DONE_SAR | self.raw
                self.conversions += 1
                log.debug("conversion %d -> raw %d", self.conversions, self.raw)
            elif not value & START_SAR:
                value &= ~DONE_SAR       # start cleared: no result pending
        self.regs[offset] = value & 0xFFFFFFFF
