# ADR 0023 — Keep the two Bluetooth clock registers the controller checks

**Status:** Accepted — 2026-09-02. Third carried patch; ADR 0022 had called its
patch the last one.

**Upstream, 2026-11-18:** Landed upstream (653451e0) in `esp-develop-9.2.2-20261109`, with a FRAC reset value of 0x02001001 where this patch had 0x02001000; the SDK's `system_reg.h` agrees with upstream. The patch is dropped (ADR 0138).

## Context

With storage working (ADR 0022), Meshtastic runs for about 40 seconds and then
trips an assertion inside `esp_bt_controller_init` and reboots:

    assert failed: 0x4212b7c4 <cached disabled>:1303 (<cached disabled>)

The literal pool at that return address names it: `bt.c`, line 1303,
expression `select_src_ret && set_div_ret`, the two calls that select the
Bluetooth low-power clock source and set its divider. Neither is in the closed
part of the controller; both are short flash functions, and a filtered
disassembly of each shows the same shape:

    btdm_lpclk_select_src   write SYSTEM_BT_LPCK_DIV_FRAC (0x600C002C),
                            return whether the selected bit reads back
    btdm_lpclk_set_div      write SYSTEM_BT_LPCK_DIV_INT  (0x600C0028),
                            return whether the low 12 bits read back

The fork's clock device (`hw/xtensa/esp32s3_clk.c`) serves the SYSTEM block.
It lists a handful of registers; for every other offset it returns zero on
read and drops the write. Those two are not listed, so both checks fail, and
the assert is deterministic.

The user chose to carry on into Bluetooth bring-up (option 1 of three: the
alternatives were turning Bluetooth off in the device's own configuration, or
stopping at storage) and to report every defect found to the fork.

## Decision

1. **The clock model keeps `SYSTEM_BT_LPCK_DIV_INT` and `_FRAC`** as ordinary
   read/write registers with their hardware reset values (divider 255;
   fractional divider 1/1 with the internal 8 MHz oscillator selected,
   `0x02001000`). The field layout is the one the fork already has for the
   ESP32-C3, which the S3 shares: the selector wrote bit 26 for the crystal,
   and that is `LPCLK_SEL_XTAL` in that layout.

2. **Carried as `qemu-patch/system-bt-lpck-regs.patch`**, applied in the same
   loop as the other two, and reported upstream as <https://github.com/espressif/qemu/issues/171>. Three files: the S3 register
   definitions, the state struct, and the model's read, write and reset.

3. **ADR 0022's "second and last" is amended**, not quietly contradicted. The
   rule stays: a patch is carried only for a defect in the fork's own logic
   that a guest cannot get past, is reported, and goes when upstream lands it.

## Consequences

**Good**

- `esp_bt_controller_init` gets past its clock setup. What it meets next is
  recorded below rather than hidden behind the assert.

**Costs, accepted**

- A third patch to re-check on every tag bump.

**Open, as of this record**

- The next stop is a bus error in ROM: a link-layer routine copies 40 bytes to
  `0x3FC00100`, below the internal SRAM the machine maps at `0x3FC80000`.
  On this chip the Bluetooth link layer lives in ROM and works against the
  baseband's exchange memory; nothing in the machine backs it, and the
  baseband's own registers at `0x60042000` are as undocumented as the radio
  pages of ADR 0019. That is the next thing to find, by the same method: a
  trace window first, to learn what the code expects, then the smallest thing
  that satisfies it.

## Alternatives rejected

**Overlay the two registers from our bus bridge.** Eight bytes on top of a
fork device. It would work only by memory-region priority, and it is exactly
the shadowing ADR 0008's amendment forbids, in a smaller coat.

**Make the clock model keep every unknown register.** Broader than the
evidence: most SYSTEM registers are plain read/write on the silicon, but a
model that returns zero on purpose for some of them would silently change
behaviour. Two named registers with documented reset values is what was shown
to be needed.
