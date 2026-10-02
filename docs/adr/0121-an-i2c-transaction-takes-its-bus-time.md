# ADR 0121 — An I2C transaction takes its bus time

**Status:** Accepted — 2026-10-02. Refines ADR 0011's controller; the I2C
counterpart of ADRs 0119 and 0120.

## Context

`esp_i2c_master` carries out a whole command list inside the guest's write of
`I2C_TRANS_START`:

- its completion bits (`TRANS_COMPLETE`, `END_DETECT`, `NACK`) are in
  `INT_RAW` at once;
- only the interrupt line waits, by a fixed `ESP_IRQ_DEFER_NS`. That delay
  exists so the interrupt does not land inside the function that started the
  transfer (ADR 0113), not because it models anything.

So a transaction takes no bus time. On a board, at 400 kHz, the touch
controller's ten-byte read is about 225 µs of SCL. At 100 kHz it is four times
that. A driver polling the keyboard and the touch panel spends that time
waiting, or gives it to other tasks.

The ESP32-S3 TRM (v1.8, 27.4) gives the timing:

- **The controller's clock.** `I2C_SCLK` is XTAL_CLK (40 MHz) or RC_FAST_CLK
  (17.5 MHz) by `I2C_SCLK_SEL`. It is divided by
  `I2C_SCLK_DIV_NUM + 1 + I2C_SCLK_DIV_A / I2C_SCLK_DIV_B` (27.4.1).
- **SCL.** A period is `I2C_SCL_LOW_PERIOD + I2C_SCL_HIGH_PERIOD +
  I2C_SCL_WAIT_HIGH_PERIOD + 3` of those cycles, when nothing stretches it.
- **START and STOP.** The start condition's setup and hold are
  `I2C_SCL_RSTART_SETUP_TIME + 1` and `I2C_SCL_START_HOLD_TIME + 1` cycles,
  and the stop condition's are `I2C_SCL_STOP_SETUP_TIME + 1` and
  `I2C_SCL_STOP_HOLD_TIME + 1`.

The field positions are ESP-IDF v4.4.6's `soc/esp32s3/include/soc/i2c_struct.h`:

| Register | Offset | Fields |
|---|---|---|
| `scl_low_period` | 0x00 | bits 0–8 |
| `scl_high_period` | 0x38 | high 0–8, wait-high 9–15 |
| `scl_start_hold` | 0x40 | 0–8 |
| `scl_rstart_setup` | 0x44 | 0–8 |
| `scl_stop_hold` | 0x48 | 0–8 |
| `scl_stop_setup` | 0x4C | 0–8 |
| `clk_conf` | 0x54 | div_num 0–7, div_a 8–13, div_b 14–19, sel 20 |

## Decision

1. **A command list lasts the bus time of the commands it ran:**
   - a restart is its setup and hold;
   - each byte written or read is nine SCL periods, eight bits and an
     acknowledge;
   - a stop is its setup and hold;
   - an END holds SCL and takes no time of its own.

2. **The completion bits it raised appear when that time has passed.** The
   exchange with the part still happens at once, so a read's bytes are in the
   FIFO already. `INT_RAW` and `INT_STATUS` show the new bits only once the
   guest's clock has passed the end. That is decided when the guest reads
   them, as for SPI's `USR` (ADR 0120).

3. **The interrupt is raised at the same instant.** It is never sooner than
   `ESP_IRQ_DEFER_NS`, which keeps the reason that delay exists.

4. **Clearing a bit clears it whether or not it has appeared yet.** A driver
   that clears before it looks has said it does not want it.

5. **The SCL frequency is logged once per change.**

## Consequences

**Good**

- An I2C transaction takes as long in guest time as on a board, so a driver's
  polling and every task waiting behind it keep a board's timeline. Under
  `--icount` it is the same every run.

**Costs, accepted**

- Not modelled:
  - clock stretching by a part;
  - arbitration;
  - the gap between a START command and its first byte.
- `I2C_SR`'s bus-busy bit still reads idle while the bus time runs. The
  firmwares' drivers wait on the interrupt bits, not on it.
- A NACKed transaction is charged its full length, though the controller
  stops at the byte that was not acknowledged.
