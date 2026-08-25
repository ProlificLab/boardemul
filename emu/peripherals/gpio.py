#!/usr/bin/env python3
"""ESP32-S3 GPIO: output levels, input levels, and the interrupt path.

Register offsets and bit positions from
components/soc/esp32s3/include/soc/gpio_reg.h.

Two windows are claimed rather than one, because `GPIO_STRAP` at 0x38 sits
between them and is the single register the fork's own GPIO model implements --
the ROM reads it at boot, so it is left alone (ADR 0010 decision 2).

The interrupt half exists because the firmware never polls its keyboard or touch
controller; it configures them once and waits on GPIO 15 and GPIO 12. Without
edge detection here, an injected key press has no way to reach the guest.
"""

from __future__ import annotations

import logging

from emu.bus import Region

log = logging.getLogger("gpio")

GPIO_BASE = 0x60004000

# Everything below GPIO_STRAP: output levels and output enables.
LOW_BASE = GPIO_BASE + 0x04
LOW_SIZE = 0x34

# Everything above GPIO_STRAP: input levels, interrupt status, pin config and
# the pin matrix. The fork models none of it, so claiming it is free and stops
# read-modify-write from being silently discarded (ADR 0008).
HIGH_BASE = GPIO_BASE + 0x3C
HIGH_SIZE = 0x1000 - 0x3C

R_OUT = 0x04
R_OUT_W1TS = 0x08
R_OUT_W1TC = 0x0C
R_OUT1 = 0x10
R_OUT1_W1TS = 0x14
R_OUT1_W1TC = 0x18

R_IN = 0x3C
R_IN1 = 0x40
R_STATUS = 0x44
R_STATUS_W1TS = 0x48
R_STATUS_W1TC = 0x4C
R_STATUS1 = 0x50
R_STATUS1_W1TS = 0x54
R_STATUS1_W1TC = 0x58
R_PCPU_INT = 0x5C
R_PCPU_NMI_INT = 0x60
R_PCPU_INT1 = 0x68
R_PCPU_NMI_INT1 = 0x6C
R_PIN0 = 0x74
PIN_COUNT = 48

PIN_INT_TYPE_SHIFT = 7
PIN_INT_TYPE_MASK = 0x7
PIN_INT_ENA_SHIFT = 13
PIN_INT_ENA_MASK = 0x1F
# Bit 0 of INT_ENA is the PRO-CPU interrupt; the others are NMI and APP CPU,
# which this firmware does not use.
INT_ENA_PRO_CPU = 1 << 0

INTR_DISABLE, INTR_POSEDGE, INTR_NEGEDGE = 0, 1, 2
INTR_ANYEDGE, INTR_LOW_LEVEL, INTR_HIGH_LEVEL = 3, 4, 5

MASK32 = 0xFFFFFFFF


class GpioOut(Region):
    """Output levels, and the output-enable registers alongside them."""

    def __init__(self, name: str = "gpio_out"):
        super().__init__(name, LOW_BASE, LOW_SIZE)
        self.level = 0          # 48 pins, bit n == pin n
        self.enable = 0
        self.changes = 0
        self.regs: dict[int, int] = {}

    def pin(self, number: int) -> int:
        return (self.level >> number) & 1

    def read(self, offset: int, size: int) -> int:
        reg = offset + LOW_BASE - GPIO_BASE
        if reg == R_OUT:
            return self.level & MASK32
        if reg == R_OUT1:
            return (self.level >> 32) & MASK32
        # W1TS/W1TC read back as zero on hardware.
        if reg in (R_OUT_W1TS, R_OUT_W1TC, R_OUT1_W1TS, R_OUT1_W1TC):
            return 0
        return self.regs.get(reg, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        reg = offset + LOW_BASE - GPIO_BASE
        value &= MASK32
        before = self.level
        if reg == R_OUT:
            self.level = (self.level & ~MASK32) | value
        elif reg == R_OUT_W1TS:
            self.level |= value
        elif reg == R_OUT_W1TC:
            self.level &= ~value
        elif reg == R_OUT1:
            self.level = (self.level & MASK32) | (value << 32)
        elif reg == R_OUT1_W1TS:
            self.level |= value << 32
        elif reg == R_OUT1_W1TC:
            self.level &= ~(value << 32)
        else:
            self.regs[reg] = value
        if self.level != before:
            self.changes += 1


class GpioInputs(Region):
    """Input levels and the interrupt path.

    `drive(pin, level)` is the entry point for anything wanting to raise a line:
    it records the level, evaluates that pin's configured trigger, and latches a
    status bit if the trigger matches. `irq_level()` then reports the line to the
    interrupt matrix on the next bridge access.
    """

    def __init__(self, out: GpioOut, static_high: set[int] | None = None,
                 irq: int = -1, name: str = "gpio_in"):
        super().__init__(name, HIGH_BASE, HIGH_SIZE, irq=irq)
        self.out = out
        # Lines with no driver that must not read low -- the panel's BUSY pin
        # would otherwise hang every wait-while-busy loop.
        self.static_high = static_high or set()
        self.inputs: dict[int, int] = {}
        self.status = 0
        self.pin_cfg: dict[int, int] = {}
        self.regs: dict[int, int] = {}
        self.interrupts = 0
        self.status_reads = 0     # guest asking which pin fired
        self.status_clears = 0    # guest acknowledging -- proof the ISR ran
        # irq_level() runs on every single bridge access, so the enabled-pin
        # mask is cached and only recomputed when a pin is reconfigured.
        self._enabled = 0

    # -- level tracking -----------------------------------------------------

    def _level(self, pin: int) -> int:
        if pin in self.inputs:
            return self.inputs[pin]
        if pin in self.static_high:
            return 1
        return (self.out.level >> pin) & 1

    def _levels(self) -> int:
        value = self.out.level
        for pin in self.static_high:
            value |= 1 << pin
        for pin, level in self.inputs.items():
            value = (value | (1 << pin)) if level else (value & ~(1 << pin))
        return value

    def drive(self, pin: int, level: int) -> None:
        previous = self._level(pin)
        self.inputs[pin] = 1 if level else 0
        if previous == self.inputs[pin]:
            return
        self._evaluate(pin, previous, self.inputs[pin])

    def pulse_low(self, pin: int) -> None:
        """Guarantee one falling edge, whatever the line was doing before.

        The touch driver's ISR does nothing but set a flag, and its read path
        returns early unless that flag is set -- so a report is only ever
        collected if an edge actually occurred.
        """
        self.drive(pin, 1)
        self.drive(pin, 0)
        self.drive(pin, 1)

    def _evaluate(self, pin: int, before: int, after: int) -> None:
        int_type = (self.pin_cfg.get(pin, 0) >> PIN_INT_TYPE_SHIFT) & PIN_INT_TYPE_MASK
        rising = before == 0 and after == 1
        falling = before == 1 and after == 0
        fires = (
            (int_type == INTR_POSEDGE and rising)
            or (int_type == INTR_NEGEDGE and falling)
            or (int_type == INTR_ANYEDGE and (rising or falling))
            or (int_type == INTR_LOW_LEVEL and after == 0)
            or (int_type == INTR_HIGH_LEVEL and after == 1)
        )
        if fires:
            self.status |= 1 << pin
            self.interrupts += 1
            log.debug("pin %d %d->%d latched (type %d)", pin, before, after, int_type)

    def _recompute_enabled(self) -> None:
        mask = 0
        for pin, cfg in self.pin_cfg.items():
            if (cfg >> PIN_INT_ENA_SHIFT) & PIN_INT_ENA_MASK & INT_ENA_PRO_CPU:
                mask |= 1 << pin
        self._enabled = mask

    def irq_level(self) -> int:
        return 1 if (self.status & self._enabled) else 0

    # -- register interface -------------------------------------------------

    def read(self, offset: int, size: int) -> int:
        reg = offset + HIGH_BASE - GPIO_BASE
        if R_PIN0 <= reg < R_PIN0 + PIN_COUNT * 4:
            return self.pin_cfg.get((reg - R_PIN0) // 4, 0)

        levels = self._levels()
        pending = self.status & self._enabled
        if reg == R_IN:
            return levels & MASK32
        if reg == R_IN1:
            return (levels >> 32) & MASK32
        if reg == R_STATUS:
            self.status_reads += 1
            return self.status & MASK32
        if reg == R_STATUS1:
            return (self.status >> 32) & MASK32
        if reg == R_PCPU_INT:
            self.status_reads += 1
            return pending & MASK32
        if reg == R_PCPU_INT1:
            return (pending >> 32) & MASK32
        if reg in (R_PCPU_NMI_INT, R_PCPU_NMI_INT1):
            return 0
        return self.regs.get(reg, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        reg = offset + HIGH_BASE - GPIO_BASE
        value &= MASK32
        if R_PIN0 <= reg < R_PIN0 + PIN_COUNT * 4:
            pin = (reg - R_PIN0) // 4
            self.pin_cfg[pin] = value
            self._recompute_enabled()
            # A level trigger on a line already at that level latches straight
            # away. An *edge* trigger must not: synthesising one here fires a
            # spurious interrupt at gpio_config() time, and since the guest has
            # no handler installed yet, nothing ever clears it -- the line stays
            # asserted and no later edge is ever seen.
            int_type = (value >> PIN_INT_TYPE_SHIFT) & PIN_INT_TYPE_MASK
            if int_type in (INTR_LOW_LEVEL, INTR_HIGH_LEVEL):
                level = self._level(pin)
                self._evaluate(pin, 1 - level, level)
            return
        if reg == R_STATUS:
            self.status = (self.status & ~MASK32) | value
        elif reg == R_STATUS_W1TS:
            self.status |= value
        elif reg == R_STATUS_W1TC:
            self.status &= ~value
            self.status_clears += 1
        elif reg == R_STATUS1:
            self.status = (self.status & MASK32) | (value << 32)
        elif reg == R_STATUS1_W1TS:
            self.status |= value << 32
        elif reg == R_STATUS1_W1TC:
            self.status &= ~(value << 32)
        else:
            self.regs[reg] = value
