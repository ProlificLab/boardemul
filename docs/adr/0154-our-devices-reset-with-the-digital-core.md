# ADR 0154 — Our devices reset with the digital core, and keep their state across a CPU reset

**Status:** Accepted — 2026-11-18. Takes up the cost ADR 0142 left: "the
devices' own registers still survive a digital reset".

## Context

Our C devices (`esp_gpio_block`, `esp_i2c_master`, `esp_ble_core`) are plain
devices outside QEMU's reset tree (ADR 0035). A machine reset reaches them
only through the handler ADR 0142 registered, which re-drives their
interrupt line and nothing else, because a handler could not tell what kind
of reset it was:

- **a digital reset** (power-on, `RTC_CNTL_OPTIONS0.SW_SYS_RESET`, the
  watchdogs' system resets) resets the GPIO, I2C and Bluetooth peripherals
  on silicon;
- **a CPU reset** (`esp_restart()`'s last step, `RTC_SW_CPU_RST`) resets
  the cores alone, and the peripherals keep their state.

So after a digital reset our devices kept everything: the matrix reset
probe found a GPIO pin's status `0x10` and its interrupt still enabled
(ADR 0142), and delivered that stale interrupt once the firmware mapped the
source.

The reset's kind is in the chip, where the ROM reads it to print its
banner: `RTC_CNTL_RESET_STATE_REG` (RTC_CNTL + 0x38), the PRO CPU's cause in
bits 5..0. The fork sets it when the guest asks for a reset, before the
machine reset runs (`esp32s3_rtc_cntl_write`: `SW_SYS_RESET` sets 3, a CPU
reset 12), and sets 1 at power-on; a device reset does not change it. ESP-IDF's
`RESET_REASON` (`esp_rom/include/esp32s3/rom/rtc.h`) names which causes
reset only the CPU: 10 (intrusion), 11 (TG0 watchdog, CPU), 12 (software,
CPU), 13 (RTC watchdog, CPU) and 17 (TG1 watchdog, CPU). Every other cause
resets the digital core.

## Decision

1. **A shared helper reads the cause.** `esp_reset_was_digital()`, in our
   `esp_board_glue.h`, reads `RTC_CNTL_RESET_STATE_REG` through the system
   address space and returns false for the five CPU-only causes, true for
   any other.

2. **On a digital reset each device returns to its reset values**:
   - the GPIO block clears what the guest configured: outputs, enables,
     interrupt status and types, and the matrix registers. Levels the
     board drives from outside are the board's and stay;
   - the I2C master clears its registers, FIFOs, interrupt bits and any
     transaction in progress;
   - the Bluetooth core clears its registers, status, FIFO and pending
     events, and stops its timers.
   Then, as ADR 0142 decided, each drives the level it now holds.

3. **On a CPU reset nothing changes** from ADR 0142.

4. **The matrix reset probe asserts it.** After `SW_SYS_RESET` the pin's
   status reads 0, and the stale edge is no longer delivered: the probe's
   count stays where the new edge alone puts it.

## Consequences

**Good**

- A firmware's second boot after a digital reset meets the peripherals a
  first boot meets, as on silicon, and no interrupt from before the reset
  survives it.

**Costs, accepted**

- A pin the guest drove falls to 0 after a digital reset, where on silicon
  it floats and a pull-up on the far side may hold it high: pull-ups are not
  modelled (ADR 0026). A part on such a line, the radio on its NRESET, sees
  its reset asserted until the firmware drives the pin again; RadioLib's
  `begin()` pulses that line as it starts in any case.
- The devices read a register of a fork device during reset. If a future
  tag moved the cause's field or stopped setting it, a digital reset would
  read as a CPU one and the devices would keep their state, as before this
  ADR. The probe would say so.
