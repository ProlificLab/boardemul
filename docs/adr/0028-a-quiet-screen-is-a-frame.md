# ADR 0028 — A quiet screen is a frame

**Status:** Accepted — 2026-09-03. Amends decision 2 of ADR 0014.

## Context

ADR 0014 samples a strip-flushing driver: a partial update is rendered when
pixels arrive and the last render is older than `interval`. The trigger is
the *next* pixels. A driver that draws a screen in strips and then waits for
the user sends no next pixels, so that screen is never written; it surfaces
only when something else is drawn over it, or at the end-of-run flush.

Meshtastic's device-ui showed the cost. Its home screen and its setup dialog
are flushed within half a second of the boot logo's last frame, and then the
interface waits. The first frame the panel model wrote of the interface was
the blanking that began thirty seconds of guest time later, with the dialog
still visible under its first black strip. A driver watching `latest.png` to
decide when to type saw the dialog forty wall seconds late and every key went
into a locked screen. The same rule hid each focus change and each typed
character until the next redraw.

## Decision

1. **A pending update is rendered once the driver goes quiet.** The panel
   records when the last pixel byte arrived; a thread inside the model renders
   what is dirty when nothing has arrived for `interval`. One frame per quiet
   period, and only when traffic left something pending.

2. **Nothing else changes.** A completed full-screen rectangle is still an
   immediate frame, a burst of strips is still sampled at most every
   `interval`, and `flush()` still emits what is pending at the end.

## Consequences

**Good**

- A screen the firmware drew and then left alone appears within one to two
  intervals of being drawn, which is what makes driving the interface from
  the input file possible: the trigger is the screen, not a later accident.
- Typed characters and focus outlines appear as they happen.

**Costs, accepted**

- A thread per panel, and a lock so the two renderers do not interleave. ADR
  0014 rejected a *sampling* thread because it would write screens no traffic
  prompted, including identical frames while the device idles. This thread
  writes only what traffic left pending, never twice, so that objection does
  not apply; the rejection of unprompted sampling stands.
- The settle frame is still a sample: if the driver pauses mid-screen for
  longer than `interval`, a half-drawn screen is written, then the finished
  one. A real camera would do the same.

## Alternatives rejected

**Have the board's input ticker call a settle method.** Works for the T-Deck,
whose board has a ticker, and silently does nothing for a board without one.
The rule belongs to the panel.

**Shorten the interval.** Does nothing for a screen followed by silence, and
floods a strip-flushing driver with frames.
