# ADR 0024 — Bluetooth bring-up: what the controller checked, and what answered it

**Status:** Accepted — 2026-09-02. Continues ADR 0023's open items.

## Context

With its clock registers kept (ADR 0023), the Bluetooth controller met a
series of further stops, each a check against hardware that is not there.
The user chose to carry on (ADR 0023, option 1). Every stop below was read
off the code that made it -- a bus error's address, a filtered disassembly of
the polling loop, or the ROM's own assert text, which prints what it compared
-- and each was answered with the least that satisfies that check. In order:

| # | What stopped it | Where it was read | Answered by |
|---|-----------------|-------------------|-------------|
| 1 | Bus error: ROM link layer copies 40 bytes to `0x3FC00100`, below the SRAM the machine maps at `0x3FC80000` | panic register dump | window `bt_em`, `0x3FC00000` up to SRAM, memory that keeps what is written |
| 2 | Spin on `SENS_SAR_TSENS_CTRL` after RF calibration | app `0x4212a1aa`: `bbci a2, 8` | temperature dump ready at once (ADR 0020 amended) |
| 3 | `assert emi.c 164, param 0 0x1000`: an exchange-memory base register read back zero | ROM `0x4000a3b8..c4`: register at `(0x1800c481 + idx) << 2`, top 14 bits times 4 must equal the table's offset | page `0x60031000` claimed; store-and-return passes |
| 4 | Spin on the core page `+0x1C` after writing bit 31 | ROM `0x4002bd73`: `l32i; bltz` | `BleCore.SELF_CLEARING`: the write consumes the bit |
| 5 | `assert lld.c 318, param 0 0x09001b00` | blob `0x4213e8c3`: `l32i +0x04; beq expected` | `BleCore.FIXED[0x04] = 0x09001B00`, the core version the link layer was built for (its ROM routines carry `rw_v9`) |
| 6 | Spin on the core page `+0x00` after writing bit 31 | blob `0x42145bac`: `l32i; extui 31,1; bnez` | the same self-clearing rule, second bit |

Between 2 and 3 the RF calibration itself ran to completion with a constant
ADC reading; a read-rate meter in the ADC model showed the sweep ending
rather than spinning, which is what it did for WiFi in ADR 0019.

With all six answered, the firmware then **reboots itself**: a software CPU
reset with nothing printed. QEMU paused at the reset (`-action
shutdown=pause`) shows core 1 in the Arduino loop task, the request coming
from the device-ui's own code right after the first write of the
configuration. That is first-boot behaviour, not a wall. The second boot is
the one that matters, and it exposed an emulator gap: **our Python
peripherals keep their state across a guest reset.** The second boot stalled
in ADC calibration reading the analog-I2C page as the first boot had left
it. Reproducing the second boot as a fresh run, with the first boot's NVS and
filesystem partitions extracted from its flash image and passed back as
overlays (ADR 0021's mechanism), gets past that: a device that has booted
once is a device with written partitions.

## Decision

1. **Two windows and one small class** in `board_tdeck.py`: `bt_em` over the
   exchange memory; `ble_core` over `0x60031000`, served by `BleCore`, a
   trace region with a table of bits a write consumes (`SELF_CLEARING`) and
   registers that read a fixed value (`FIXED`). Each entry names the loop or
   assert it came from. A third window, `bt_unknown` at `0x60042000`, the
   page the blob's literals name next to the exchange memory, has seen no
   access yet and stays a plain window.

2. **`EXTRA_REGIONS` gets a class map** instead of a name test in `build()`,
   so a region is served by what knows it, and a plain window by default.

3. **The second boot is reproduced from written partitions**, not by
   surviving the reset: extract `nvs` and `spiffs` from the run's flash image
   and overlay them on a fresh run. Until peripherals follow a guest reset,
   that is the honest way to run a firmware past its first reboot.

4. **Every instrument used is kept where it is cheap**: the ADC read-rate
   meter (an INFO line per thousand reads), the QMP snapshot scripts under
   `logs/dbg/` (a task-control-block scan whose saved-frame layout is still
   wrong, and the pause-at-reset stack reader that found the reboot's
   caller), and the exit code in the run result.

## Consequences

**Good**

- The controller's initialisation checks all pass and it proceeds into its
  own start-up; the firmware reaches its second boot with its configuration
  saved.

**Open, as of this record**

- **Peripheral reset on guest reset.** The bridge devices reset with the
  machine; the Python side does not learn of it. A reset message from the
  bridge to the bus server, and a `reset()` on regions and I2C/SPI devices,
  is the feature; ADR to come.
- **Host-controller sync.** After its init the controller's traffic on the
  core page has to be read next: whether it programs the core and waits for
  an interrupt nothing raises, or has already stopped.
- **QEMU dies with signal 11** when a run follows a long run within seconds
  (`guest exited (code -11)`), in QEMU or in our devices; not yet caught with
  a backtrace. Runs spaced apart do not show it.
- **Heavy instrumentation ends runs early**: verbose bus logging and
  unfiltered translation logging both produced guest resets (one seen as the
  IPC task's stack canary). Filtered disassembly and the PC-watch are safe.

## Alternatives rejected

**Model the BLE core.** Undocumented, ROM and blob code against it, and six
answers so far were each one register or one bit. The core's behaviour is
being read one check at a time because that is the only source there is.

**Turn Bluetooth off in the configuration.** Offered and declined by the
user (ADR 0023); it remains the fallback if the controller's start-up turns
out to need the radio's interrupts.

**Keep whole-page store-and-return for everything unknown.** It is what the
windows do, and it is exactly where a self-clearing bit and a version
register broke it: what the hardware does on its own has to be modelled, and
only that.
