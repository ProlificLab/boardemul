#!/usr/bin/env python3
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

from emu.bus import Region

log = logging.getLogger("adc")

R_MEAS1_CTRL2 = 0x0C
START_SAR = 1 << 17
DONE_SAR = 1 << 16
DATA_MASK = 0xFFFF

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

    def read(self, offset: int, size: int) -> int:
        return self.regs.get(offset, 0)

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
