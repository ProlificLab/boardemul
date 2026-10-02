# ADR 0119 — Audio runs at its sample rate

**Status:** Accepted — 2026-10-02. Refines ADR 0016's decision 2 and the
transmit side added after it.

## Context

`esp_i2s_master` moves audio on a fixed 5 ms tick, whatever the guest asked
for:

- **receive:** one descriptor's worth of samples per tick;
- **transmit:** a whole queued list per tick, up to its end marker.

ADR 0016 chose the tick because "what the firmware waits for is the transfer".
But a transfer has a length in time, and the guest set it. A 4 KB buffer at
16 kHz, 16-bit stereo, is 64 ms of sound. Here it took 5 ms to record, and a
list of any length played in 5 ms. So a firmware recording for a second, or
waiting for a sound to finish before it goes on, runs ahead of a board by
whatever ratio its buffers happened to give. Its boot timeline then differs
from silicon's, which is what the guest-clock work (ADRs 0104, 0112–0115) has
been removing everywhere else.

The rate is in the registers, and the ESP32-S3 TRM (v1.8, chapter 28) says how
to read it:

- **The unit's clock (28.6)** is the source divided by `N + b/a`:
  - the source is selected by `CLK_SEL`: 0 is XTAL_CLK (40 MHz), 1 is
    PLL_D2_CLK (240 MHz), 2 is PLL_F160M_CLK (160 MHz), 3 is the MCLK input;
  - `N` is `CLKM_DIV_NUM`, except that 0 means 256 and 1 means 2;
  - `a` and `b` follow from `x`, `y`, `z` and `yn1`, by the formulas given there.
- **BCK** is that clock divided by `BCK_DIV_NUM + 1`.
- **A WS period** is `HALF_SAMPLE_BITS × 2` BCK cycles (28.9.2.1).
- **In TDM mode** only the enabled channels, among the first
  `TDM_TOT_CHAN_NUM + 1`, take data from memory (28.9.2.1). Standard stereo
  I2S is TDM with two channels.
- **Each channel takes the valid data width** (28.9.1.1), set by `BITS_MOD`
  and `24_FILL_EN`.

ESP-IDF v4.4.6's `hal/esp32s3/include/hal/i2s_ll.h`, which the firmwares here
link, writes the counting fields as the value less one:
`tx_half_sample_bits = half_sample_bits - 1`, `tx_bck_div_num = val - 1`,
`tx_tdm_tot_chan_num = total_num - 1`, `tx_bits_mod = data_bit - 1`. So
`HALF_SAMPLE_BITS` holds half the WS period less one, whatever the TRM's
one-line description suggests.

## Decision

1. **Each direction's byte rate is computed from its own registers.**
   - WS frequency = BCK / (2 × (`HALF_SAMPLE_BITS` + 1)).
   - Bytes per frame = the enabled channels × the bytes of valid data: 1, 2,
     3, or 4 with 24-bit fill, from `BITS_MOD` + 1.
   - Bytes per second = WS frequency × bytes per frame.
   - The fractional divider is decoded by the TRM's formulas. IDF v4.4.6
     writes `y` one larger than those formulas when `yn1` is 0. IDF v5.1.2's
     `i2s_ll_tx_set_mclk` writes `denom % div_z`, which is the TRM's formula,
     so Espressif's later code agrees with the TRM about what the hardware does
     with the bits.

2. **A transfer completes when its bytes have been clocked.**
   - A receive descriptor is filled, and its interrupt raised, once the time
     to clock its bytes has passed since the last one.
   - A transmit list is taken once its own bytes have had time to go out,
     counted from when the previous list was taken. Its end-of-frame interrupt
     then comes when that buffer has been played, as a driver waiting on it
     expects.
   - The timer is set for that instant rather than polled.

3. **When the rate cannot be known, the old 5 ms tick stays**, and the run's
   log says why, once. That covers:
   - slave mode, where BCK comes from the codec, which is not modelled;
   - PDM mode;
   - the MCLK input as source;
   - an inactive clock;
   - a configuration with no enabled channel.

4. **A buffer the CPU still owns when it falls due** is an overrun, as before.
   The receiver tries again on the next tick, and the time it waited is not
   made up afterwards.

5. **The computed rate is logged once per start**, with its parts, so a run
   says what sample rate its firmware chose.

## Measured

The vendor UnitTest build 250620 records on I2S1 and plays on I2S0. Both come
out as 16-bit stereo at 16,002 Hz, 64,006 bytes a second.

It asks for 16 kHz. IDF v4.4.6 chooses N = 39 plus 1/16 for that, but with
its extra one in `y` the divider reads back as 39 plus 1/17. Decoded as the
TRM says, that is 16,001.5 Hz. By the TRM, that is also what this build's
audio runs at on a board.

## Consequences

**Good**

- Recording and playback take as long in guest time as on a board. Under
  `--icount` they take exactly that long, every run.
- A firmware that sizes a recording by bytes, or waits for its sound to drain,
  keeps a board's timeline.

**Costs, accepted**

- The FIFO's depth is not modelled. A transmit list's end-of-frame comes when
  its last byte has been clocked, not when it entered the FIFO a few words
  earlier.
- Slave mode keeps the fixed tick until a codec model drives BCK.
- Samples are still silence in and nothing out (ADR 0016).
