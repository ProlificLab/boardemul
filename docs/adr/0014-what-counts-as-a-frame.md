# ADR 0014 — Decide what a frame is, for a display that does not have any

**Status:** Accepted — 2026-08-27

## Context

The first board's panel is e-paper. It has a refresh command, and refresh is a
physical event: the controller drives the film and the image changes. Rendering
a PNG when that command arrives is not a modelling choice, it is a transcription
of what the hardware does.

The T-Deck's ST7789 has nothing of the sort. It scans controller RAM out to the
glass continuously, whatever is in there. There is no instant the hardware calls
a frame, so the emulator has to invent one, and inventing badly goes wrong in
both directions: render per SPI segment and one screen becomes five identical
PNGs, render only on some end-of-run and an interactive session shows nothing.

Two smaller questions came with it, both of which look like image transforms and
are not:

- **MADCTL's BGR bit.** The datasheet defines it as which colour filter the
  glass in front of the controller uses, so that ordinary RGB-ordered host data
  comes out right on a BGR panel. It is a statement about the hardware, not an
  instruction to the host.
- **INVON.** These IPS panels are natively inverted, so drivers switch inversion
  on to arrive back at normal.

Applying either literally produces a wrong picture, and the wrongness is
plausible enough to survive review -- the first render of Meshtastic's boot
screen came out a yellow-green that looked entirely reasonable until it was
compared against Meshtastic's own brand colour.

## Decision

1. **A completed full-screen rectangle is a frame.** When a window covering the
   whole display has been filled, render immediately. This is the case that
   matters and it is unambiguous.

2. **Everything else is sampled, not transcribed.** If a driver flushes in
   strips it never completes a full-screen window, so the model snapshots at
   most every `interval` seconds while the framebuffer is dirty. That is what a
   camera pointed at the device would capture, which is the honest analogue for
   a display that is always on.

3. **Never sample part-way through a full-screen fill.** A snapshot taken mid-
   fill shows a half-drawn screen nobody would have seen. A full-screen window
   is worth waiting for.

4. **`flush()` at the end of a run** emits whatever is pending, so a session
   that stops mid-update still shows its last screen.

5. **BGR and INVON are recorded and not applied.** Both are reported in the run
   summary, so anyone can see they were sent, and neither transforms the image.

## Consequences

**Good**

- Meshtastic's two full-screen fills render as exactly two frames.
- A strip-flushing driver cannot produce a hundred PNGs per screen, so the
  frame sequence stays readable as a sequence of screens.
- The colour is right, and checkable: Meshtastic's boot screen renders as its
  own brand green rather than something merely plausible.

**Costs, accepted**

- **`interval` is a judgement, not a measurement.** Half a second is fast enough
  to watch and slow enough not to flood; nothing about the hardware picks it.
- A driver that redraws the whole screen in strips gets sampled rather than
  captured exactly, so a frame may show two consecutive states in different
  parts of the screen. A real camera would do the same, but it means frame
  hashes for such a firmware are not reproducible the way the e-paper goldens
  are.
- The BGR reading is an interpretation of the datasheet. It is corroborated by
  the rendered colour matching Meshtastic's brand green, which is evidence but
  not proof; a board whose driver pre-swaps its own colours would need this
  revisited rather than assumed.

## Alternatives rejected

**Render on every segment.** Transcription with no invention, and it produces
five identical PNGs for one screen and hundreds for an LVGL-style flush.

**Render only when the run ends.** Correct and useless: no way to watch a boot,
and no way to see the screen change in response to input.

**A background thread sampling the framebuffer on a timer.** The truest model of
a continuously-scanned display, and it would render screens that no traffic ever
prompted, including long runs of identical frames while the device sits idle.
Sampling on the traffic that actually changes something gets the same pictures
without a thread or the noise.

**Apply BGR and INVON literally.** Defensible from a naive reading of the bits,
and it renders the wrong colours -- which is exactly the kind of wrong that
looks fine until something checks it.
