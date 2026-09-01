# ADR 0015 — Leave I2S unmodelled, and record why the obvious route fails

**Status:** Superseded by [ADR 0016](0016-i2s-receive-path.md) — 2026-09-01.
The reference manual showed that four of the facts this record reasons from are
wrong: the block is I2S1 rather than I2S0, the transfer is a capture rather than
a playback, and the register and link bits were read with the transmit layout.
Kept because the failure modes it describes are real and were expensive.

## Context

The T-Deck's vendor firmware initialises its display, recognises our touch
controller and keyboard, draws its splash screen, and then stops in its audio
test. It is waiting for an I2S transfer to finish, and on this chip the samples
move by GDMA, so the wait ends when the DMA engine reports EOF rather than when
anything is written to an I2S register.

This looked small, because almost all of it already exists. The fork models GDMA
(`hw/dma/esp_gdma.c`) with a peripheral-facing API, already knows `GDMA_I2S0` as
a peripheral, and SHA and AES are worked examples of driving it. Nothing
connects I2S to it, and that seemed to be the whole gap.

It was attempted. What follows is what the attempt established, because the
attempt is the expensive part and none of it is written down anywhere else.

- **`esp_gdma_get_channel_periph` cannot be used as a trigger.** It matches a
  channel whose peripheral is right **or** which is merely started, so it hands
  back a channel that was configured for I2S0 and never launched. Draining one
  of those walks a stale descriptor address and clears owner bits wherever it
  lands. The firmware died on a null load before printing a line.

- **Finding the channel directly works.** Walk the five channels, take the one
  whose `GDMA_PERI_SEL_REG` is `GDMA_I2S0` and whose `GDMA_LINK_REG` has START
  (bit 21) set, with a non-zero address (bits 0..19).

- **The drain cannot be done in pieces.** `esp_gdma_read_channel` needs an exact
  byte count and always restarts from the head of the link, so the count has to
  come from walking the descriptor chain in guest memory first and summing the
  length fields.

- **The DMA start alone is too early a trigger.** The driver arms GDMA and
  configures I2S afterwards, so completing the transfer on the strength of the
  channel being started lands while the driver is still building its state.
  Gating on the transmitter as well -- the bit written last before it blocks --
  moves the crash later but does not remove it.

- **With all of that right, completing the transfer still kills the firmware.**
  One drain, channel 0, head `0x3fca88c0`, 2048 bytes: every value plausible,
  and the guest panics anyway. So something about what the completion *means* --
  which status bits, what the descriptor write-back should look like, whether
  the receive side matters too -- does not match what the driver expects.

## Decision

Leave I2S unmodelled. Keep its window claimed as an unmodelled region so that
read-modify-write behaves (ADR 0008), and let the firmware stop where it stops.

An emulator that crashes the firmware is worse than one that stalls it. The
stall is honest and legible; the crash was neither, and the two versions of it
found so far were both silent memory corruption that surfaced somewhere else
entirely.

## Consequences

**Good**

- The vendor firmware reaches its splash screen and recognises both input
  devices, which is where it stops today and is a state that can be reasoned
  about.
- No guessed register semantics are baked into the C tier, where this project's
  most expensive bugs have always lived.

**Costs, accepted**

- The vendor firmware's LVGL interface never runs, so interaction has to be
  driven through Meshtastic, whose own responsive window is short.
- The work above is thrown away except as this record. That is the trade: the
  next attempt starts from five established facts rather than from nothing.

**What would settle it**

The ESP32-S3 technical reference manual, for the I2S and GDMA status bits; or
instrumenting the guest's own interrupt handler to see what it reads after the
completion and which of those reads is wrong.

## Alternatives rejected

**Ship it anyway.** It made the firmware crash instead of stall, and the first
version corrupted guest memory in a way that surfaced as a null dereference in
unrelated startup code. That is precisely the silent wrongness the test suite
exists to prevent.

**Guess harder at the register bits.** The bits are guessable and the guesses
are checkable only by whether the firmware survives, which is a slow and
ambiguous oracle. Two guesses were made here and both were wrong in ways that
took a rebuild and a boot to discover.
