# ADR 0148 — The Max board carries the chip's I2S controllers and Bluetooth core

**Status:** Accepted — 2026-11-18. Applies ADR 0137's reasoning to two more
of the ESP32-S3's own blocks.

## Context

ADR 0137 wired the T-Deck's WiFi MAC into the Max board, because the WiFi
MAC is a property of the ESP32-S3 the two boards share, not of the board
around it. Two more of the chip's blocks are in the same position:

- **The two I2S controllers** (`esp_i2s_master`, ADRs 0016, 0119, 0146), at
  0x6000F000 and 0x6002D000. On the Max, 0x6002D000 is a claimed window
  named `i2s0` (ADR 0008), which is I2S1's address under I2S0's name, and
  0x6000F000 is not claimed at all.
- **The Bluetooth link-layer core** (`esp_ble_core`, ADRs 0025, 0045), at
  0x60031000 on interrupt source 8. The Max has nothing there.

The Max carries an ES8311 codec, a speaker and a microphone (its factory
firmware's Test page passes ES8311), and the chip's Bluetooth radio. Its
factory firmware does not touch any of these blocks while it boots: in a
90-second boot on 2026-11-18 the bus log holds no access to 0x6000F000,
0x6002D000 or 0x60031000. So wiring them changes nothing the boot does,
and gives what reaches them later a model rather than a window that only
remembers writes.

Since ADR 0146, the I2S controllers can write what is played to a WAV file
and record from one. On the Max both options say they are ignored, and so
does `--ble-air`, because the blocks are not there.

## Decision

1. **The Max board wires both I2S controllers and the Bluetooth core,
   unchanged**, as the T-Deck does: the same devices at the same addresses
   and interrupt, since they are the chip's. The `i2s0` claimed window goes.

2. **`--audio`, `--mic` and `--ble-air` work on the Max** as on the T-Deck,
   and the lines saying they are ignored go.

3. **Nothing about the Max's own parts changes.** The ES8311 still answers
   only its identity on I2C (ADR 0006): the codec's own registers, volume
   and routing are not modelled. What the I2S controller plays is what the
   firmware sends towards the codec.

## Consequences

**Good**

- A Max firmware that plays a sound, records, or starts Bluetooth meets the
  chip's blocks instead of windows that only remember writes.
- The settings sheet offers the Max what it offers the T-Deck.

**Costs, accepted**

- None of the Max's firmware is known to reach these blocks yet, so this is
  checked by the boot not changing and by the probes, not by a Max firmware
  using them.
