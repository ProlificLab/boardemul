# ADR 0144 — The image says which PSRAM line mode it was built for

**Status:** Accepted — 2026-11-18. Amends ADR 0007, whose re-test after the
tag bump of ADR 0138 this records.

## Context

ADR 0007 presents PSRAM in quad line mode by default, though the board's
part is octal, because the Max's factory firmware only enumerates its PSRAM
that way. Its amendment found the T-Deck's Meshtastic build does the
opposite: it only enumerates in octal. So the mode became a launch option,
`--psram-octal`, and "which setting a given image needs is now something to
determine per firmware". ADR 0007 also asks to be re-tested first after any
QEMU tag bump.

**The re-test**, on `esp-develop-9.2.2-20261109` (2026-11-18), each image
booted for 45 s in each mode (`tools/psram_mode.sh`):

| image | quad | octal |
| --- | --- | --- |
| Max factory V3.1 | `PSRAM enabled` | `PSRAM ID read error`, init failed |
| Meshtastic 2.7.10 | `opi psram: PSRAM ID read error`, then a StoreProhibited panic | `PSRAM enabled` |
| UnitTest Debug 251222 | `PSRAM enabled` | `PSRAM enabled` |
| UnitTest 250620, 251113 | no PSRAM line | no PSRAM line |

The tag changed nothing: each image enumerates in the mode it did before.

**Why per image.** ESP-IDF 4.4 carries two PSRAM drivers for the ESP32-S3,
and a build links the one its `sdkconfig` chose. Each logs under its own tag:
the quad driver (`spiram_psram.c`) as `psram`, the octal one (`opi_psram.c`)
as `opi psram`. ESP-IDF 5 renames them `quad_psram` and `octal_psram`. The
tag strings are in the image:

- the Max image holds `psram` and no `opi psram`: it was built for quad;
- the Meshtastic image holds `opi psram`: it was built for octal;
- the UnitTest builds hold neither, and work in either mode or print nothing.

So each image enumerates in exactly the mode its own driver speaks. That is
ADR 0007's "property of the firmware", now read off the firmware rather than
found by trying.

It matters most where nobody is there to type the flag. The web page's
create form has no line-mode choice, so a Meshtastic device created from the
browser was a quad device, and panicked on every boot.

## Decision

1. **When nothing says otherwise, the line mode is the image's.** `run.py`
   reads the firmware image, or a device's flash, for the drivers' tags:
   - `opi psram` or `octal_psram` selects octal;
   - `psram` (as a whole string) or `quad_psram` selects quad;
   - neither, or both, keeps quad, ADR 0007's default.
   The launch says which, and why: `psram   : octal, the image's driver`.

2. **A person can still choose.** `--psram-octal` forces octal, as before,
   and `--psram-quad` forces quad. A device created with `--psram-octal`
   keeps forcing it. A device created without it, which until now meant
   quad, now means "the image's".

3. **ADR 0007's divergence stands for the Max.** Its image's driver is the
   quad one, so the Max is still presented in a line mode its part does not
   have. Where the image's driver and the board agree, as for Meshtastic on
   the T-Deck, the emulator now matches silicon without being told.

## Consequences

**Good**

- Meshtastic boots from the web page and from a bare `run.py` without a
  flag nobody would guess. The Max's firmware is unchanged.
- The re-test ADR 0007 asks for is recorded, with its method, so the next
  tag bump can repeat it.

**Costs, accepted**

- The choice rests on log tag strings. An image built with its log strings
  stripped carries no tag, and gets quad. That is the default it had anyway,
  and the flags remain.
- The whole image is scanned at every launch, 16 MB at most. That is a few
  milliseconds beside a QEMU start.
