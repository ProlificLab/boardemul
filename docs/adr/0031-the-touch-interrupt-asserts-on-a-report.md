# ADR 0031 — The touch interrupt asserts on a report and idles between

**Status:** Accepted — 2026-09-05. Supersedes the resting-low rule of ADR 0029.

## Context

ADR 0029 settled a conflict between two touch drivers by keying the interrupt
line's level on the trigger mode a firmware writes, and resting the line low
when none is written. It rested on a claim about the vendor firmware: that it
writes mode 0 and reads the line as pressed when high. That claim was wrong.

The wire says what the vendor firmware actually does. It reads the mode
register once at start-up and never writes it; its driver, SensorLib, only
logs what the register says. In a hundred and fifty seconds it addressed the
controller twenty-six times, all of them during initialisation, and never
again. So neither firmware writes a mode, both fall into ADR 0029's
"unknown" case, and the line rests low for the whole run and never changes.

A line that never changes tells an interrupt-driven firmware nothing. The
vendor firmware reads the controller's configuration and then waits; it is
waiting for an edge that our model never produced. It read no touch at all,
in any run, before or after ADR 0029.

The register's own value had never been learned either. The model
zero-initialised its register array, so 0x804D read back as rising edge — the
model was telling the vendor's driver one thing while the line did another.

## Decision

1. **The line asserts while the controller has something to report** and idles
   otherwise, so a press and a lift each produce an edge.

2. **A release is a report.** The line stays asserted until the host has taken
   a status with no finger in it. Going idle the moment the finger lifts would
   leave a driver that reads only while asserted holding the press for ever.

3. **The part's configuration is stated, not left at zero.** 0x804D holds
   low-level: an interrupt that asserts low, which is the usual arrangement
   for a line with a pull-up on it and the one both observed drivers work
   with. A driver that reads the register is now told what the line does.

4. **A firmware that writes its own mode is still followed.** That much of
   ADR 0029 stands; only its resting rule is replaced.

## Consequences

**Good**

- Both drivers are served by one line behaviour rather than two conventions:
  the level-reading driver sees the finger, the interrupt-driven one gets its
  edges.
- The register and the line agree, so a driver that reads the mode to decide
  how to wait is told the truth.
- Meshtastic's touch, verified under ADR 0030, still works: it reads while the
  line is asserted, which is exactly when there is a report.

**Costs, accepted**

- The mode value is stated by the model rather than read off a real part. It
  is the value consistent with both drivers and with a pull-up, which is
  evidence, not measurement.

## Alternatives rejected

**Keep resting low and special-case the vendor firmware.** The model would
have to know which firmware is running, which ADR 0006 exists to avoid.

**Assert only on the press.** Cheaper, and it strands a driver that reads only
while asserted in a permanent press, because the lift is never delivered.

**Pulse briefly per report period.** Closest to a datasheet description, and
it needs a width this project cannot justify without a scope: too short and a
polling driver misses every press, too long and it never idles.
