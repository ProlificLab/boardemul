# ADR 0020 — The vendor interface takes input; the last two walls were an ADC and a pin level

**Status:** Accepted — 2026-09-02. Amends ADR 0019's open items.

## Context

ADR 0019 got the vendor firmware through `esp_wifi_start()` and into its LVGL
interface, and left one thing open: the interface drew but did not consume
input. Every keyboard read in a run happened before it appeared, and the touch
controller was never read at all.

Two separate causes, found by two different instruments.

**The interface stopped being serviced at all.** Frame timestamps showed every
frame written in the first ~50 s and none after; the WiFi scan result printed at
59 s was a coincidence that sent the first look down the wrong path. A sampling
profiler settled it -- a hook on our I2S device's 5 ms timer that logs each
core's PC, costing the guest nothing, where `-d int` slowed it so much that a
five-minute run never reached the state under study. After the scan CPU0 was
idle and **CPU1, which runs `loop()`, sat at one instruction in every sample**:

    0x42068a3a:  l32i  a8, a3, 12       ; SENS_SAR_MEAS1_CTRL2
    0x42068a3d:  extui a8, a8, 16, 1    ; MEAS1_DONE_SAR
    0x42068a40:  beqz  a8, 0x42068a34   ; spin until done

That is `analogRead()`: pulse `MEAS1_START_SAR` (bit 17), wait for
`MEAS1_DONE_SAR` (bit 16). The firmware reads its battery voltage for the status
bar, nothing modelled the SAR ADC, and the conversion never finished -- so the
LVGL task never got another turn, and with it went every input read. The radio
had nothing to do with it.

**Touch was wired backwards.** The vendor driver's `isPressed()` is
`digitalRead(INT) == trigger level`, and it configured this GT911 for RISING --
pressed means the pin is HIGH, and stays high while the finger is down. The
board drove a low pulse, inherited from the Max's CST328, which can never read
as pressed.

## Decision

1. **A SAR ADC model, `emu/peripherals/adc.py`,** at `DR_REG_SENS_BASE` with a
   one-shot conversion: the rising edge of START sets DONE and puts a sample in
   the same register's low half. Everything else on the page is stored. The
   sample is a modelling choice and says so -- there is no battery -- and its
   default reads as a healthy pack after calibration and the board's divider.
   *Amended 2026-09-02:* the page's temperature sensor answers the same way.
   Meshtastic, after RF calibration for Bluetooth, sets `TSENS_DUMP_OUT` and
   spins on `TSENS_READY` (app `0x4212a1aa`: `bbci a2, 8`, read off the
   loop); a reading is ready as soon as one is asked for, reported and not
   stored, and its value is as much a choice as the battery's.

2. **The region is exactly `0x400`.** The first cut claimed `0x1000` and ran
   into IO_MUX at `0x60009000`, which the fork models. That is the third time
   ADR 0008's amendment has bitten, in a new form: a claim can shadow a real
   model by *size* as well as by base. A test now checks every claimed region
   against the pages the fork models at the pinned tag.

3. **The touch interrupt is a level, active high**, held while pressed.

4. **Two investigation hatches stay**, both off unless named in the
   environment, and both documented where they live: `BOARDEMUL_PC_WATCH` in
   the bus bridge logs the PC on reads of given addresses or ranges;
   `BOARDEMUL_PC_SAMPLE` in the I2S device samples both cores' PCs at 5 ms.
   Between them they found every wall in ADR 0019 and this one.

## Consequences

**Good**

- The T-Deck is what the goal asked for: the vendor's own firmware, unmodified,
  runs its full interface and answers keys and taps. In one 110-second run: 740
  frames at a steady ~10 per second, 754 keyboard polls with every injected
  character delivered and echoed by the firmware itself, 66 touch reports.
- The battery shows a plausible voltage rather than a stall.

**Costs, accepted**

- The ADC returns a constant. A firmware that reads an analog *signal* -- a
  microphone level, a light sensor -- gets the same number every time.
- The ADC is SoC silicon and by ADR 0011 belongs in the C tier. It lives in
  Python because a battery read is a handful of accesses a second and the
  socket cost is nothing; if a firmware ever samples fast, that changes.

## Alternatives rejected

**Keep looking at the radio.** The scan printing right when the interface froze
made the WiFi wait the obvious suspect, and every instrument aimed at the radio
pages agreed it was busy. The profiler was the first instrument that asked where
the *main task* was rather than where the traffic was.

**Pulse the touch line both ways.** Would satisfy either polarity of an
edge-triggered driver and still not this one, which reads a level.
