# ADR 0007 — Present PSRAM in quad line mode, though the board's part is octal

**Status:** Accepted — 2026-08-21

## Context

Every firmware image failed PSRAM detection identically:

```
E (275) psram: PSRAM ID read error: 0x00000000, PSRAM chip not found
              or not supported, or wrong PSRAM line mode
```

This blocked everything. The factory demo died in an `IntegerDivideByZero`
shortly afterwards, and the e-paper example hung outright.

The board carries an ESP32-S3R8, whose 8 MB PSRAM is an **octal** part, and the
fork is invoked accordingly with
`-global driver=ssi_psram,property=is_octal,value=true`. The fork's model is not
obviously at fault: it is wired to SPI1 CS1 correctly, `is_octal` does reach it,
and it carries a full set of APS6408L mode registers.

Time was spent looking for the bug in the wrong place — our SPI0 window, the
eFuse blob, the flash image — before testing the last clause of the error
message rather than the first two. Presented in **quad** line mode, the same
unmodified firmware reports:

```
[   630][I][esp32-hal-psram.c:96] psramInit(): PSRAM enabled
```

The most likely explanation is that the fork's SPI1 model does not clock octal
DDR the way ESP-IDF 4.4.6's ROM-based detection drives it, and this firmware is
built against 4.4.6 while the fork is validated against 5.x. That explanation is
**unverified** — what is verified is only that quad works and octal does not.

## Decision

1. **Quad is the default line mode.** `--psram-octal` selects the physically
   accurate configuration for anyone who wants to retest it, and is documented
   as currently non-working.

2. **The divergence is recorded as a known lie.** The emulated part answers in a
   line mode the real part does not use. Guest code that inspects PSRAM timing
   or line mode will see something the hardware would never report. Nothing in
   the boot path does, so it costs nothing today.

3. **This does not get "fixed" by changing our models.** The mismatch is between
   the fork's SPI1 and the firmware's IDF version. If it is ever worth resolving
   properly, the fix belongs upstream in the fork or in a future tag bump, not
   in a workaround of ours.

## Consequences

**Good**

- PSRAM is detected and enabled, which unblocked the factory demo past its
  divide-by-zero and the e-paper example past its hang.
- One command-line flag, no code, no model of our own to maintain.

**Costs, accepted**

- An emulator that is knowingly wrong about a hardware detail. Recorded here
  rather than left as a surprise.
- The real cause is unproven. If a future tag bump changes PSRAM behaviour, this
  ADR is the first thing to re-test.
- Firmware built against IDF 5.x may want octal and fail on the default. The
  flag exists for that case.

## Alternatives rejected

**Keep octal and model SPI0's PSRAM path ourselves.** Our SPI0 window is already
intercepted, so this was tempting. Rejected because the ID exchange happens on
SPI1, which also carries flash — taking it over would mean reimplementing the
flash command path to get PSRAM detection, which is a large amount of work to
avoid a one-word flag.

**Patch the fork's PSRAM or SPI1 model.** Directly contradicts ADR 0001's
boundary rule, and would make every future tag bump a merge.

**Leave PSRAM broken and work around it.** What was happening by default, and it
is not survivable: the factory demo crashes and the e-paper example hangs.
