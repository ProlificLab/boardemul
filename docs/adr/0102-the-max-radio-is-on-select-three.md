# ADR 0102 — The Max's radio is on chip select 3

**Status:** Accepted — 2026-10-01. Corrects ADR 0043's radio pins, which that
ADR itself marked as inferred.

## Context

ADR 0043 gave the T-Deck Max's radio chip select 9 and interrupt 45. It said
plainly that both were inferred: 9 was the T-Deck's number and went low before
one early burst, and in every boot measured then the firmware never addressed
the radio.

With the USB serial port modelled (ADR 0100), the factory demo now reports on
each part as it brings it up, and the radio's line is:

    [SX1262] Initializing ... failed, code -2

That is RadioLib's chip-not-found. The radio model recorded no commands at all,
so the firmware never selected it.

Two sources now say where the radio is, and they agree:

- **The vendor's pin map.** `lib/TDeckMaxBoard/src/TDeckMaxBoard.h` in
  Xinyuan-LilyGO/T-Deck-MAX. It carries the software version `v3.1-260313`,
  which is this factory image (`T-Deck-MAX_V3.1_20260313.bin`):

      #define BOARD_LORA_CS   3
      #define BOARD_LORA_BUSY 6
      #define BOARD_LORA_RST  4
      #define BOARD_LORA_INT  5
      #define BOARD_EPD_RST   9

- **The firmware.** With the GPIO trace on (ADR 0043's hatch), the factory
  demo drives pin 3 1,183 times and pin 4 21 times while the radio is being
  initialised. Pin 9 goes 1, 0, 1 exactly once: a reset pulse, and the header
  says it is the panel's reset. ADR 0043 read that pulse as the radio being
  probed.

The same header gives the card's select as 48, but this image drives 8 and the
card mounts on 8 (`SD Card Size: 32MB`). The image is the board being emulated,
so the card stays on 8. The header is used where the firmware confirms it.

## Decision

1. **The radio is on chip select 3**, and its interrupt (DIO1) on pin 5, as the
   header says. Pin 4, its reset, is the firmware's output and needs nothing
   here.

2. **BUSY (pin 6) is left undriven**, which reads low: idle, as on the T-Deck,
   whose model has never driven its own BUSY. Modelling the time a command
   keeps BUSY high is separate work for both boards.

3. **The select-9 entry goes.** Pin 9 is the panel's reset, which the panel
   model has no use for.

## Consequences

**Good**

- The Max's radio can be found. Everything the T-Deck's radio does on the air
  (ADRs 0036 to 0039) is now available to this board too.

**Costs, accepted**

- The vendor header and the vendor image disagree about the card. The image
  wins, and that disagreement is written down here rather than averaged away.
