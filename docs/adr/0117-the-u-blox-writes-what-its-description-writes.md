# ADR 0117 — The u-blox writes what its interface description writes

**Status:** Accepted — 2026-10-02. Refines ADR 0105 decision 2; applies ADR
0116's way of stamping a report to the T-Deck Max's receiver.

## Context

ADR 0116 set the L76K's sentences against Quectel's specification and found
them wrong in form. The T-Deck Max's u-blox model (ADR 0105) was built the same
way and shares the same formatter. u-blox's *M10 SPG 5.10 Interface
Description* (UBX-21035062 R02, `logs/ref/ublox_m10_interface.*`) is the
source for it:

- **Table 69, CFG-MSGOUT defaults:** on the UART, GGA, GLL, GSA, GSV, RMC and
  VTG are each on once per solution, and the rest are off. The model sends
  RMC and GGA.
- **The examples (2.7):**

      $GPGGA,092725.00,4717.11399,N,00833.91590,E,1,08,1.01,499.6,M,48.0,M,,*5B
      $GPGLL,4717.11364,N,00833.91565,E,092321.00,A,A*60

  Minutes have five decimals ("ddmm.mmmmm"); the model writes four. HDOP has
  two decimals; the model writes `0.9`.
- **A receiver standing still (2.5.3)** writes speed `0.000` and leaves the
  course empty:

      $GNRMC,014230.00,A,4722.80340,N,00831.68218,E,0.000,,120477,,,A,V*14

  The model writes `0.00,0.00`.
- **Invalid or unknown data (2.5.6):** "By default the receiver will not
  output invalid data. In such cases, it will output empty fields". Its
  example with no time is `$GPGLL,,,,,,V,N*64`.

And the model stamps a report with whenever Python polled it, to the whole
second. Its first report comes one second after the clock was first heard. So
the second a stamp names depends on when the host's thread ran, which ADR 0116
has just stopped the L76K doing.

## Decision

1. **Reports fall on the board's whole seconds**, the first a second after
   power-on, and each is stamped with its own instant, as in ADR 0116. A fix
   missed while the reader stalled is skipped, not queued.

2. **It sends the default messages that a position and a time decide**: GGA,
   GLL, RMC and VTG, in message-ID order (0xF0 0x00, 0x01, 0x04, 0x05). The
   description gives no order. GSA and GSV are on by default too, but they
   describe a sky the model does not have, as ADR 0116 found for the L76K.

3. **Each sentence takes the description's form:**
   - five decimals of a minute;
   - UTC as `hhmmss.ss`, which it already was;
   - HDOP with two decimals;
   - standing still: speed `0.000`, course empty, in RMC and in VTG alike.

   Indoors, every field it cannot know is empty, as 2.5.6 says. GGA keeps
   `0,00,99.99` for quality, satellites and HDOP, as it did. Talker `GN` stays,
   since the part combines constellations.

## Consequences

**Good**

- The Max's receiver writes the same fields, to the same precision, as the
  part's documentation, and what it streams by default matches Table 69 except
  for the sky.
- Under `--icount` its stamps are the same in every run.

**Costs, accepted**

- GSA and GSV are missing, so a firmware counting satellites in view gets
  nothing. The Max's demo takes its satellite count from GGA, which has one.
- Four sentences instead of two: some 230 bytes a second with a fix, against
  about 140 before. At 9600 baud that is under a quarter of the line.
- VTG standing still is not shown in the description. It follows RMC's
  standing-still fields, since both report the same course and speed.

## Alternatives rejected

**Fix only the decimals.** The same reading of the description says which
sentences are on. Leaving two of the four out would keep a difference the
description states plainly.
