# ADR 0018 — Leave the radio unmodelled, and record how far the trail goes

**Status:** Accepted — 2026-09-02

## Context

With audio working, the T-Deck's vendor firmware stops in `setupWiFi()`.
`WiFi.begin()` calls `esp_wifi_start()`, which never returns: the debug build
logs `WIFI_READY` and then never `STA_START`, and `setupUI()` is the
second-to-last line of the vendor's `setup()`, so its interface is never built.
That is why only the splash is ever drawn.

Three things were tried, and the order matters because two of them were dead
ends worth not repeating.

**Profiling by interrupt PC.** The interrupt log records the PC of every
exception, which makes a free sampling profiler. It found two hot loops, one of
them a ROM helper that sets `PS.INTLEVEL`. Both turned out to be lock churn in
whichever task was *running*. The blocked task is by definition not running, so
this cannot see it. A dead end, but a cheap one.

**A full memory-region trace (ADR 0004).** 64 GB and 672 million accesses,
written to the container's own disk because the bind mount could not keep up.
Worth every byte: 377,671 of the last 400,000 accesses land on one address,
`0x6000e050`, in the machine's catch-all region. Everything else is ordinary
timer traffic.

**Answering that register.** The firmware writes a little configuration to
`+0x000`, `+0x044` and `+0x048`, then polls `+0x050` about 1,100 times a second,
for as long as it is left running. Four different answers were tried -- store
what was written, always zero, all ones, and a countdown -- and every one
behaves identically. It is not a simple wait-for-this-value.

That block is undocumented. It appears in neither the fork's register map nor
IDF's `reg_base.h`, sitting in the gap between UHCI1 at `0x6000C000` and I2S at
`0x6000F000`. And the fork models no radio at all: there is no WiFi, PHY or RF
model anywhere in `hw/` or `include/`.

## Decision

Leave the radio unmodelled, and claim `0x6000E000` so the stall is visible
rather than swallowed by the catch-all.

The remaining work is not modelling a peripheral, it is reverse-engineering what
a closed-source RF calibration blob expects from an undocumented register block.
That is a different kind of project from this one, and an open-ended one: the
polling loop gives no gradient to follow, because every wrong answer looks
exactly like every other wrong answer.

## Consequences

**Good**

- The stall is legible. The window is claimed, so the next person sees a poll on
  a named radio register rather than a misleading tail somewhere else -- which
  is what happened twice during this investigation, first pointing at SPI0's
  PSRAM calibration registers and then at the busiest address rather than the
  blocking one.
- The investigation was not wasted: the debug build's own logging showed our
  GT911 reporting `Resolution : X = 0 Y = 0` and `MaxTouchPoint:0`, because the
  vendor driver reads the product-information block at 0x8146 rather than the
  config block at 0x8048. Both are fixed and tested; the driver now reads
  320x240 and one touch point.

**Costs, accepted**

- The vendor firmware never builds its interface, so it is not the one to drive
  for anything interactive. Meshtastic remains that.
- Anything on this board that needs WiFi, and therefore the Meshtastic mesh
  itself, is out of reach.

## Alternatives rejected

**Keep guessing at the register.** Four answers were tried and the loop is
indifferent to all of them. Guessing further is unbounded and the failure signal
carries no information.

**Model the PHY properly.** It is a binary blob performing RF calibration
against registers Espressif does not document. No QEMU fork models ESP32 WiFi,
including their own, which is a reasonable indication of the size of it.
