# ADR 0142 — Our devices re-drive their interrupt after a reset

**Status:** Accepted — 2026-11-18. Follows up a difference ADR 0138 recorded
in the QEMU tag it adopted. Its last cost, registers that survive a digital
reset, is taken up by ADR 0154.

## Context

Three of our own QEMU devices raise an interrupt source through the ESP32-S3
interrupt matrix: `esp_gpio_block`, `esp_i2c_master` and `esp_ble_core`. Each
remembers the level it last drove (`irq_level`) and forwards only a change:

    if (!s->irq_line || level == s->irq_level) {
        return;
    }

None of them has a reset handler.

The tag adopted in ADR 0138 (`esp-develop-9.2.2-20261109`) took over our
interrupt-matrix fix (ADR 0101) with one difference. On a peripheral reset the
matrix now clears the level it holds for every source
(`esp32s3_intmatrix_reset_hold`: `bitmap_zero(s->irq_levels, ...)`), where ours
kept them. Upstream's reason, in its own comment, is that not every peripheral
model lowers its interrupt on reset, and on silicon no source is asserted after
a system reset.

When the matrix gets that reset is the SoC's to decide
(`esp32s3_soc_reset`):

- **A peripheral reset** happens at power-on, and on a digital system reset:
  `RTC_CNTL_OPTIONS0.SW_SYS_RESET`, written by `esp_rom_software_reset_system()`,
  and the watchdogs' system resets. The matrix is cleared.
- **A CPU reset**, the last step of `esp_restart()` and of a panic
  (`RTC_SW_CPU_RST`), resets the cores alone. The matrix keeps its levels.

After a peripheral reset, one of our three devices that held its line high
still records `irq_level = 1`, and the matrix records 0. The next time the
device computes 1, the two values are equal and nothing is forwarded. The
interrupt is lost until the source first drops to 0, which for a latched
status may be never, because the firmware never sees it to clear it.

On silicon this cannot happen. The digital reset resets the GPIO, I2C and
Bluetooth peripherals too, and the source and the matrix agree that nothing
is asserted. Our devices are not reset. They are plain devices outside the
reset tree (ADR 0035), and a machine reset handler cannot tell a peripheral
reset from a CPU reset. Both arrive as the same machine reset.

`esp_usb_serial_jtag` and `esp_wifi_mac` already register a reset handler and
drive their line on reset. The Python bus re-evaluates its interrupts after a
reset (`emu/bus.py`).

A probe measured it on the GPIO block (`tools/matrix_reset_probe`, built with
the vendor builds' ESP-IDF 4.4, 2026-11-18). It raised an edge interrupt with
the CPU masked, then reset the digital system with `SW_SYS_RESET`. After the
reset the pin's status was still `0x10` and its interrupt still enabled,
which silicon would have cleared:

- **A new edge taken without the driver was lost**: "taken: no (before 0,
  after 0)".
- **Through the driver it was not.** ESP-IDF's `gpio_intr_enable` clears a
  pin's status before enabling its interrupt, which lowers the source and puts
  the device and the matrix back in step. The probe saw the status go from
  `0x10` to `0` across that one call. On silicon a firmware has to enable the
  pin again after a digital reset, because the reset disabled it, so firmware
  built on the driver never meets the lost interrupt.

The defect is therefore latent: a firmware that takes an interrupt it
configured before the reset, without enabling it again, meets it on the
emulator and nowhere else. It is still a trap, and it is the emulator's own.

## Decision

1. **Each of the three devices registers a machine reset handler that
   forgets the level it last drove and drives the level it computes now.**
   After a peripheral reset the matrix learns the source's real level. After
   a CPU reset the matrix already had it, and since it forwards only a change
   to a CPU line, driving the same level again does nothing.

2. **The probe guards it**, as `tools/restart_probe` does for ADR 0123:
   - `tools/matrix_reset_probe` raises a GPIO edge interrupt with the CPU's
     interrupt masked, so the source is asserted and not yet taken.
   - It then resets the digital system through `SW_SYS_RESET`.
   - After the reset it takes a new edge on the same pin, without the driver.
   - `tests/test_matrix_reset_probe.py` asserts that the new edge reaches the
     CPU. It also asserts that the driver clears a pin's status as it enables
     it, the fact that keeps firmware away from the defect.

## Consequences

**Good**

- A source asserted across a digital reset can no longer silence itself:
  the matrix and the device agree again as soon as the reset is over.

**Costs, accepted**

- The devices' own registers still survive a digital reset, where silicon
  resets them. A GPIO status bit latched before the reset is therefore still
  set after it, and is now delivered once the firmware maps the source: the
  probe's ISR ran once for it ("before 1, after 2"). That is a stale
  interrupt the firmware clears, where it used to be a lost one.
  Resetting the devices' registers on a digital reset needs the reset's kind,
  which they cannot see; that is a decision of its own.
