# ADR 0120 — An SPI transfer takes its bus time

**Status:** Accepted — 2026-10-02. Refines ADR 0032's controller. Same idea as
ADR 0119, for the GP-SPI master.

## Context

`esp_spi_master` carries out a transaction the moment the guest sets
`SPI_USR`, and clears the bit at once. The guest's drivers start a transfer,
then poll `USR` until it clears; here they never wait.

So every SPI transfer takes no guest time, at any clock:

- **the display:** a full 320 × 240 frame is 153,600 bytes, 1.23 Mbit. At
  40 MHz that is about 31 ms of bus time on a board. Here it takes nothing,
  so the interface draws faster than the silicon can, and its other work runs
  in time the bus would have used;
- **the radio and the card:** their transfers are short and slower clocked,
  but still take no time.

That is the gap ADR 0119 closed for I2S, on the other bus the firmwares lean
on hardest.

The ESP32-S3 TRM (v1.8, 30.7) gives the clock:

- `fSPI_CLK = fclk_spi_mst / ((SPI_CLKCNT_N + 1)(SPI_CLKDIV_PRE + 1))`;
- or `fclk_spi_mst` itself when `SPI_CLK_EQU_SYSCLK` is set;
- `clk_spi_mst` is PLL_F80M_CLK (80 MHz) when `SPI_MST_CLK_SEL` is 1, and
  XTAL_CLK (40 MHz) when it is 0, in `SPI_CLK_GATE_REG`.

A transaction is a sequence of states (30.5.8.1): preparation, command,
address, dummy, data out, data in, done. Each enabled state runs until a
counter reaches its length field, and the done state ends at once unless
`SPI_CS_HOLD` is set. The field positions are ESP-IDF v4.4.6's
`soc/esp32s3/include/soc/spi_struct.h`:

- `SPI_USER_REG` enables each state: `cs_setup` (bit 7), `cs_hold` (6),
  `usr_command` (31), `usr_addr` (30), `usr_dummy` (29), `usr_miso` (28),
  `usr_mosi` (27), `doutdin` (0) for full duplex, and `fwrite_dual`, `quad`
  and `oct` (12–14) for the data lines out.
- `SPI_USER1_REG` gives the lengths: `usr_dummy_cyclelen` (0–7),
  `cs_setup_time` (17–21), `cs_hold_time` (22–26), `usr_addr_bitlen` (27–31),
  each its value less one.
- `SPI_USER2_REG` has `usr_command_bitlen` (28–31), also less one.
- `SPI_CTRL_REG` has the lines for command, address and reading: `fcmd_*`
  (8–10), `faddr_*` (5–7), `fread_*` (14–16).
- `SPI_MS_DLEN_REG` has the data length in bits, less one.

## Decision

1. **A transaction lasts the cycles its states take at the SPI clock.**
   - Preparation and done: `cs_setup_time + 1` and `cs_hold_time + 1` cycles,
     when enabled.
   - Command and address: their bits over their lines.
   - Dummy: `usr_dummy_cyclelen + 1` cycles.
   - Data: the bits over the lines. A full-duplex transfer clocks its bits
     once. A half-duplex one with both a write and a read clocks the write,
     then the read.

2. **`USR` stays set for that long.** The bytes still reach the part, and its
   answer the FIFO, when the transfer starts. What changes is when the guest
   is told it is over.
   - `USR` reads as set until the guest's clock passes the transfer's end.
   - That is decided when the guest reads it, on its own clock. A timer would
     clear it only when the host's main loop got to it. On the wall clock
     that can be tens of microseconds late, several times a short transfer.

3. **A transfer started while one is running starts when that one ends.** A
   driver that waits for `USR` never does this. Only a driver that does not
   wait would see it.

4. **The clock source** is `SPI_MST_CLK_SEL` when the guest has turned the
   module clock on (`SPI_MST_CLK_ACTIVE`). Before that, the register's own
   description is used: `fSPI_CLK = fAPB_CLK/(PRE + 1)/(N + 1)`, at the
   80 MHz APB clock.

5. **The clock is reported once per change**, so a run says how fast each
   bus went.

## Consequences

**Good**

- The display's frame rate is bounded by its bus, as on a board. So is the CPU
  time an interface gives to everything else, and with it the firmware's
  timeline.
- Under `--icount`, a transfer's length in guest time is the same every run,
  and the same as on silicon, up to what is not modelled below.

**Costs, accepted**

- The guest now spins on `USR` for the length of each transfer, which costs
  host time in proportion. That is the CPU time the firmware spends on
  silicon too.
- Not modelled:
  - the gap between transactions that the controller adds itself;
  - clock phase and edge settings, which move no time;
  - DMA transfers through the controller, which these firmwares do not use
    on this bus.
- Segments still carry the guest time the transfer started (ADR 0112). A
  part that acts on a command at the end of the window gets that time, early
  by the transfer's length.
