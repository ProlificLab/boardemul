# ADR 0005 — Forward unmodelled MMIO out of QEMU over a Unix socket

**Status:** Accepted — 2026-08-21

## Context

The firmware reaches application code and then stops, polling I2C registers
that no device services. Every peripheral this board actually has — the
e-paper over SPI2, the keyboard, touch, I/O expander and PMIC over I2C, the
LoRa radio over SPI — sits behind a controller the fork does not model.

ADR 0001 committed to putting all device intelligence outside QEMU, in Python,
with a C footprint confined to files we add. This is that mechanism.

## Decision

1. **One new C file, `esp_bus_bridge.c`, and it lives in *our* repository**, at
   `qemu-patch/`. `tools/build_qemu.sh` copies it into `hw/misc/` and appends a
   single line to `hw/misc/meson.build` at build time. The QEMU working tree is
   never edited by hand, and ADR 0001's boundary is enforced by construction
   rather than by discipline: there is nothing of ours in the QEMU tree to
   forget about.

2. **No Kconfig entry.** Adding the file unconditionally to `system_ss` is one
   line instead of three and costs nothing in a single-target build. Total
   modification to the QEMU tree: **one line**.

3. **The device is a plain `TYPE_DEVICE`, not a `SysBusDevice`.** A sysbus
   device cannot be created with `-device` unless the machine sets
   `has_dynamic_sysbus`, which would mean editing `hw/xtensa/esp32s3.c`. A
   plain device has no such restriction; its `realize()` claims MMIO by calling
   `memory_region_add_subregion_overlap()` on the system memory region.

4. **It claims specific windows, never the whole peripheral space.** Each
   instance takes `base`, `size` and `priority`, so we intercept I2C0 without
   also intercepting the UART that the modelled machine handles correctly. The
   priority sits above the catch-all `esp32s3.iomem` so our region wins.

5. **The wire protocol is fixed-size binary, not JSON.** Twelve bytes out, four
   bytes back, no framing and no parsing:

   ```
   request : u8 op (0=read, 1=write) | u8 size | u16 pad | u32 addr | u32 value
   response: u32 value
   ```

   Every MMIO access pays a round trip, so the cost of parsing is paid
   millions of times. JSON here would be a self-inflicted wound.

6. **Writes are acknowledged, not fire-and-forget.** An unacknowledged write
   would let the guest race ahead of the model and read state that has not been
   updated yet. Correctness before throughput.

7. **The C file contains no device behaviour.** No register decode, no state
   machines, no timing, no interrupt logic. If it ever needs to know what a
   register means, the boundary has been breached.

## Consequences

**Good**

- Device models are Python: editable, testable, and restartable without a C
  rebuild. Iterating on a display controller becomes seconds rather than
  minutes.
- Bumping the QEMU tag stays a two-line diff, because the one line we add is
  appended by a script rather than committed into their tree.

**Costs, accepted**

- Every intercepted access is a blocking round trip on the vCPU thread while
  QEMU holds the big lock. Throughput will be poor by emulator standards and
  entirely adequate for one device driving a slow display.
- A crashed or slow Python side hangs the guest rather than failing cleanly.
  Acceptable while both are on the same machine and under development.
- Nothing enforces decision 7 automatically. It is checked by reading the file,
  which is short enough to read.

## Alternatives rejected

**Model the peripherals in C inside QEMU.** Faster at runtime and the direct
contradiction of ADR 0001. It is also much slower to develop against, which
matters more here than emulation speed does.

**Shared memory instead of a socket.** Lower latency, and it gives up the clean
request/response ordering that decision 6 depends on. Revisit only if
throughput is ever demonstrated to be the limiting factor.

**One bridge instance covering the whole `0x60000000` window.** One command
line argument instead of several, and it would intercept the UART, timers and
eFuse that the fork already models correctly — throwing away the main benefit
of building on the fork at all.
