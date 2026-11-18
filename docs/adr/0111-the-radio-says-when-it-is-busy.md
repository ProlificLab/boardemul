# ADR 0111 — The radio says when it is busy

**Status:** Accepted — 2026-10-01. Closes ADR 0106 item 7 and the work ADR
0102 decision 2 set aside.

## Context

An SX126x has a BUSY pin. The datasheet tells the host to wait for it to be low
before every command. RadioLib does that: in `Module::SPItransferStream` it
waits while BUSY is high before a transfer and again after it, and gives up
after 1000 ms with `SPI_CMD_TIMEOUT`. It also waits on BUSY after a calibration
and after a staged transmission starts, and it wakes a sleeping chip with a NOP
and then waits for it.

The model never drove the pin, so it always read low (ADR 0102). Every command
was answered as if the chip were instant. A reset, a 3.5 ms calibration, and a
5 ms TCXO start-up all took no time at all.

The pins, from the vendors' pin maps:

- T-Deck: BUSY 13, NRESET 17 (`examples/UnitTest/utilities.h`).
- T-Deck Max: BUSY 6, NRESET 4 (`TDeckMaxBoard.h`, ADR 0102).

Python cannot drive this pin itself. A host-driven GPIO level is a one-way
message, and it arrives on the host's clock (`emu/peripherals/gpio.py`).
RadioLib reads BUSY one microsecond after NSS rises, so a level sent from
Python would race the guest's read. The chip-select release is a one-way
message too, so the model has had no way to answer "busy for this long".

### What the datasheet says

Source: Semtech DS.SX1261-2.W.APP, Rev. 1.2, June 2019.

- **Commands.** A write command raises BUSY within T_SW of NSS rising, at most
  600 ns. A read command leaves BUSY low (8.3.1).
- **Mode switches.** The time from NSS rising to BUSY falling, typical
  (Table 8-2):

  | From      | To        | µs  |
  |-----------|-----------|-----|
  | STBY_RC   | STBY_XOSC | 31  |
  | STBY_RC   | FS        | 50  |
  | STBY_RC   | RX        | 83  |
  | STBY_RC   | TX        | 126 |
  | STBY_XOSC | FS        | 40  |
  | STBY_XOSC | TX        | 105 |
  | STBY_XOSC | RX        | 62  |
  | FS        | RX        | 41  |
  | FS        | TX        | 76  |
  | RX        | FS        | 15  |
  | RX        | TX        | 92  |

- **Sleep.** BUSY is high for the whole of sleep. A falling NSS edge wakes the
  chip, and BUSY falls once it is in STBY_RC: 340 µs for a warm start and
  3500 µs for a cold one (Table 8-2). `sleepConfig` bit 2 selects warm
  (Table 13-2).
- **Reset and power-up.** BUSY is high in reset (Table 8-3) and during
  start-up, until the chip is in STBY_RC (8.2.2, 9.1). Start-up runs the
  power-on calibration including the image (9.2), which is the cold start's
  3500 µs.
- **Calibrate.** Calibrating all blocks takes 3.5 ms, and BUSY is high
  throughout (13.1.12).
- **TCXO.** `SetDIO3AsTCXOCtrl` sets a delay in units of 15.625 µs. Switching
  from STBY_RC into a mode that needs the 32 MHz clock takes that much longer
  (13.3.6). A complete reset is the only way back to crystal operation (4.1.4).

The datasheet gives no time for these:

- ordinary writes (parameters, registers, the buffer);
- `CalibrateImage`;
- any switch not in Table 8-2: into standby, into CAD, or back into the same
  mode;
- BUSY rising while the chip handles its own interrupts (8.3.1).

## Decision

1. **The SPI controller keeps a part's BUSY line on the guest's clock.** A chip
   select can be given a BUSY pin and a reset pin as properties: `busy_gpio`,
   `busy1_gpio`, `busy2_gpio`, and `reset_gpio`, `reset1_gpio`, `reset2_gpio`.
   For such a select, the controller asks the part how long it is busy each
   time the select is released or the reset pin moves. It then holds the pin
   high for that long on QEMU's virtual clock. The part decides the length;
   the controller only keeps the time. No knowledge of the part enters C
   (ADR 0011). The pin rises at the release, inside the datasheet's 600 ns.

2. **On the wire:**
   - A release on a select with a BUSY line is segment type 3, and it waits
     for an answer `<II`: `high_ns, wake_ns`.
   - A move of the reset pin is type 4, with `dc` set to the pin's new level,
     and it is answered the same way.
   - `high_ns = 0xFFFFFFFF` holds BUSY high until the select next falls; it
     then falls `wake_ns` after that edge. `wake_ns = 0xFFFFFFFF` means a
     select does not end the hold either; only the next reset edge does.
   - A release can only lengthen a hold. A reset edge replaces it.
   - A select without a BUSY line is unchanged: type 2, one way.

3. **The SX1262's hold times are the datasheet's and nothing else:**
   - **Mode switches:** Table 8-2, looked up by the mode the chip was in and
     the mode asked for. When a TCXO is configured, a switch out of STBY_RC
     adds its delay.
   - **Calibrate:** 3.5 ms.
   - **SetSleep:** held until the select next falls, then 340 µs warm or
     3500 µs cold.
   - **Reset:** high while NRESET is low, and 3500 µs after it rises. Reset
     also returns the chip to its reset state: STBY_RC, no interrupts flagged
     or routed, no TCXO, and registers, buffer and modulation as the model
     powers up with them. The run's own records, such as packets sent
     and heard, are not chip state and are kept.

4. **Nothing the datasheet leaves out is invented.** Ordinary writes,
   `CalibrateImage`, switches outside Table 8-2, CAD and internal interrupt
   handling get no hold. A measurement can fill these in later. Until then
   they are what the model has always done.

5. **The controller reports the line.** It reports how many holds there have
   been, the longest, the total, and how many windows opened while BUSY was
   high outside a wake from sleep. It writes this report after the 1st, 2nd,
   4th, 8th hold and so on. A host is meant to wait for BUSY, so opening a
   window while it is high is worth seeing. RadioLib does it on purpose with
   the NOP it sends before every standby. After a reset that NOP lands while
   the chip is still starting up, which is harmless.

## Consequences

**Good**

- The firmware waits for the radio as it would on silicon: 3.5 ms after a
  reset, 3.5 ms for a calibration, and the TCXO's start-up each time it leaves
  STBY_RC for receive or transmit.
- A firmware that does not wait for BUSY now shows up in the log, where before
  nothing could tell.
- Reset puts the chip back to its reset state. Before, the radio kept
  everything across a reset, including across a firmware reboot (ADR 0099).

**Costs, accepted**

- Each radio command now takes one more round trip: its release is answered.
  Bringing a radio up takes on the order of a thousand commands, so this adds
  milliseconds.
- *(Closed by ADR 0112.)* The packet still leaves at the release on the
  host's clock (ADR 0037), not when the PA ramp ends. The difference is the
  76–126 µs in Table 8-2, against airtimes of tens of milliseconds.
- *(Closed by ADR 0112.)* After a transmission or a single reception, the
  chip still stays in the mode it was in rather than dropping to its fallback
  mode. RadioLib calls `standby()` after both (`finishTransmit`,
  `finishReceive`), so no switch time it sees depends on this.
- *(Closed by ADR 0113 on a timed air, and by ADR 0141 on the wall-clock air.)* After a CAD the chip is still left
  in RX, though the datasheet returns it to STBY_RC once a sweep finds
  nothing (13.4.7). That makes the transmission
  Meshtastic starts straight after its listen-before-talk an RX-to-TX switch
  (92 µs) rather than STBY_RC-to-TX (126 µs plus the TCXO). Correcting only
  the mode was tried, and it made the two-node mesh lose packets:
  - A packet arriving between a node's CAD and its transmission had been
    received only because the model left the chip in RX.
  - On silicon the CAD would have detected that packet, and the node would
    have backed off to receive it. This model's CAD detects nothing.

  So the mode moves together with CAD detection, in the guest-clock air work
  (ADRs 0112, 0113).
