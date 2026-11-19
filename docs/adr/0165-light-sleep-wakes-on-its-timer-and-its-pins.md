# ADR 0165 — Light sleep wakes on its timer and its pins

**Status:** Accepted — 2026-11-19. Adds a carried QEMU patch (ADR 0017),
`qemu-patch/rtc-light-sleep.patch`, and a line from our GPIO block (ADR 0151)
to Espressif's RTC controller.

## Context

A Meshtastic device on the page froze on its splash screen in about half of
its boots once it had run a few times: no frame, no serial line, QEMU busy.
Both cores' registers, read from QEMU's monitor while frozen, said where:

- core 1 at `0x40380604`, polling `0x60008044` for its low two bits, after
  writing 3 to `0x6000804c` and bit 31 into `0x60008018`;
- core 0 at `0x4037f260` with PS.INTLEVEL 5, parked in the IPC interrupt
  that stalls the other CPU.

That is ESP-IDF's `rtc_sleep_start()`, read against Espressif's register
header (`soc/esp32s3/.../rtc_cntl_reg.h`): clear the SLP_REJECT and
SLP_WAKEUP interrupts in `RTC_CNTL_INT_CLR_REG` (+0x4c), set
`RTC_CNTL_SLEEP_EN` in `RTC_CNTL_STATE0_REG` (+0x18), and wait until
`RTC_CNTL_INT_RAW_REG` (+0x44) says the chip woke or refused. The firmware
had gone into light sleep. Espressif's model of the RTC controller handles
a dozen registers and none of these, so nothing ever woke it.

What it had armed, read once the sleep was modelled: wake on GPIO 0 low (the
T-Deck's trackball press), GPIO 45 high (the radio's DIO1, a packet), and the
RTC timer about 109 s ahead. That is Meshtastic's own light sleep; a touch
does not wake it on the board either.

### A first design, measured and dropped

The registers were first modelled as overlay windows from our GPIO block on
top of Espressif's controller, as ADR 0152 overlays the SENS block. It woke
the node. It also broke an unrelated test: the vendor T-Deck build's WiFi
join through the uplink (`test_wifi_uplink`) failed six times in eight with
the windows mapped and never in seven without them, even with windows the
firmware never touched (a trace of every access to them showed eight, all at
boot, all writes of what the old model already ignored). Mapping windows on
that page under two translation threads disturbs that firmware's join, by a
mechanism not found. The registers therefore go where the hardware has them:
inside the controller's own model, and nothing new is mapped.

## Decision

1. **Espressif's RTC controller model gains light sleep**, a carried patch
   to `hw/misc/esp32s3_rtc_cntl.c` and its header, at the offsets of
   Espressif's register header: `SLP_TIMER0/1` (+0x04, +0x08), `STATE0`
   (+0x18: SLEEP_EN, and the SLP_REJECT and SLP_WAKEUP status bits),
   `WAKEUP_STATE` (+0x3c, WAKEUP_ENA at bits 31:15), `INT_ENA`, `INT_RAW`,
   `INT_ST`, `INT_CLR` (+0x40 to +0x4c), `SLP_REJECT_CONF` (+0x68,
   SLEEP_REJECT_ENA at bits 29:12), `SLP_REJECT_CAUSE` (+0x128) and
   `SLP_WAKEUP_CAUSE` (+0x130).

2. **Setting SLEEP_EN starts a sleep**, with the sources the firmware enabled,
   numbered as `soc/rtc.h` numbers them:
   - **the timer** (`RTC_TIMER_TRIG_EN`, bit 3): a QEMU timer at the virtual
     time the model's own RTC count reaches `SLP_TIMER0/1`'s 48-bit target;
   - **the pins** (`RTC_GPIO_TRIG_EN`, bit 2): a GPIO input to the model,
     `gpio-wakeup`, high while a pin set to wake is at its level.

   A source already firing refuses the sleep if the firmware enabled refusal
   for it (SLP_REJECT and the cause), and ends it at once otherwise. A wake
   clears SLEEP_EN, raises SLP_WAKEUP and records its source in
   `SLP_WAKEUP_CAUSE`, which `esp_sleep_get_wakeup_cause()` reads. A reset
   ends a sleep.

3. **Our GPIO block drives `gpio-wakeup`**: high while any pin has
   `GPIO_PINn` WAKEUP_ENABLE (bit 10) and is at the level INT_TYPE names (4
   low, 5 high), recomputed when a level or a pin's configuration changes.
   It finds the input by name and does without it, as before, on a tree
   without the patch.

4. **The patch is carried and listed in `tools/build_qemu.sh`** with the
   other two. It is a candidate for Espressif's tree; offering it waits on
   the owner's go-ahead.

## Consequences

**Good**

- A firmware that light-sleeps wakes as the board does: on its timer, and
  when the pins it chose change, such as the T-Deck's trackball under
  Meshtastic. The frozen node of the Context wakes on a trackball press.
- Nothing is mapped that was not before; the vendor build's WiFi join passes
  as it did.

**Costs, accepted**

- A third carried patch, to Espressif's own model.
- While asleep the cores still run: the firmware's wait loop spins, and a
  sleeping device costs the host as much as an awake one. ESP-IDF's sleep
  accounting takes the running clocks in its stride: it advances its timer
  by what the RTC measured minus what its own timer saw.
- Other wake sources (UART, WiFi, Bluetooth, touch pads, EXT0/EXT1 through
  the RTC pads, ULP) never wake it; a firmware relying on one still waits,
  now at a named place. Deep sleep, which powers the chip down and wakes
  through a reset, is not modelled.
- Why mapped windows disturbed the WiFi join is not known. The measurements
  are in `logs/` scripts of the day, not kept as tests.
