# ADR 0135 — A browser is the window

**Status:** Accepted — 2026-10-19. The web UI becomes the primary front
door; the Tk window remains as the lightweight viewer it already was.

## Context

The viewer's contract has always been client-shaped: frames arrive as PNGs
in a directory, input leaves as JSON lines in a file, and anything that
reads one and writes the other is a working window (ADR 0010, ADR 0103's
backlight state file). The Tk app is one such client — and building the
device chooser inside it (ADR 0134) meant hand-rolling everything a
browser does for free: anti-aliased shapes painted a pixel at a time,
colour tuples kept away from colour strings, a ripple re-implemented as a
pixel loop, and a Tcl that intermittently refuses to start.

The draft that replaced it (`logs/web-draft.html`) was static HTML with the
same palette and took a fraction of the effort, and it could show things Tk
could not easily give: a filter field, a detail card, a drag-and-drop
firmware drop zone, a frame-history scrubber, an expanding serial console.

## Decision

1. **`emu/web.py` serves the interface on localhost** — standard-library
   `http.server`, no framework, no CDN, no build step. The page is one
   embedded HTML file under `emu/web/`. It binds 127.0.0.1 and opens the
   browser itself. `python -m emu.web` is the front door.

2. **The API is the folder format and the run protocol, verbatim.**
   Devices list, create, start, stop, flash and remove map onto
   `emu/device.py`; a started run is a child `emu/run.py --device` whose
   frames are served as PNGs and whose input is written into the run's own
   `input.jsonl` by the same `InputWriter` the Tk window used. Frames are
   fetched by long-poll so a panel refresh appears the moment it happens.

3. **The keyboard's brains stay in Python.** The page posts host key events
   and drawn-key presses; the server runs the chord logic (ADR 0127's
   `TDeckTyping`) per run, so there is one tested implementation of "what a
   character means on this board", not a JavaScript copy of it.

4. **The Tk window is not deleted.** It still watches any frame directory
   and takes input, which keeps it useful for scripts and for someone who
   wants a native window. New interface work goes to the web: features that
   only make sense there — the serial console, the frame-history scrubber,
   drag-and-drop firmware — are not ported back.

## Consequences

**Good**

- The interface is made of the web's own materials: real text, real CSS,
  real focus and hover states, DPI-perfect, and reachable over SSH port
  forwarding when the emulator runs elsewhere.
- The chooser's craft problem dissolves: state layers and ripples are CSS,
  not pixel loops.

**Costs, accepted**

- Two front doors until the Tk window retires; both read the same protocol,
  so neither can drift from the emulator.
- A browser tab is not an app window: it shares the browser's chrome and
  focus model. Billed as a feature (it is), noted as a change.
