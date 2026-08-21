# ADR 0009 — A desktop viewer that watches a file and draws its own device

**Status:** Accepted — 2026-08-21

## Context

Frames on disk prove the emulation works but are a poor way to watch it. What
was wanted was something that looks like the device.

Two constraints shaped every choice. The project depends on the Python standard
library only, and the emulator runs inside the devcontainer (ADR 0002) while a
GUI has to run on the host — a boundary a VM's Unix sockets do not cross.

## Decision

1. **tkinter.** It is standard library, so it costs no dependency. Tk 8.6 reads
   PNG natively and `PhotoImage.zoom()` does integer scaling, which is exactly
   right for a 1-bit panel: nearest-neighbour, no resampling, no blur.

2. **The viewer watches `logs/frames/latest.png` and nothing else.** No import
   of the emulator, no socket, no shared process. That side-steps the container
   boundary entirely — the panel model writes a file, the app reads it — and
   means the app can be started before, during or after a run. The cost is that
   it is one-way: this is a viewer, not yet an input device.

3. **The skin is drawn with Canvas primitives, not a photograph.** A bundled
   image would put a binary asset in a repository whose only dependency is the
   standard library, and would pin the skin to one resolution. Drawing it keeps
   the whole thing in the same units as the panel, so nothing drifts out of
   register between scales.

4. **All geometry is expressed in panel pixels and multiplied by `--scale`**, so
   the screen well is always exactly 240x320 times the scale and the image lands
   on an exact pixel boundary.

5. **The poll loop must not be able to stop.** The reschedule sits in a
   `finally`, a change is detected by `(mtime_ns, size)` rather than mtime
   alone, and a half-written PNG leaves the stamp uncommitted so the next tick
   retries. A viewer that dies silently on one bad read is worse than no viewer.

## Consequences

**Good**

- The emulated panel can be watched live, in something that reads as the device.
- Zero coupling: the app cannot break the emulator and does not care where it
  runs.
- Nothing to install.

**Costs, accepted**

- **One-way.** Keyboard and touch input need a channel back into the guest,
  which the bridge's request/response protocol does not have. That is the next
  piece of work, not a defect in this one.
- Polling at 250 ms is not free and is not instant. Both are irrelevant for a
  panel whose real refresh takes about a second.
- A portrait panel gets tall: scale 3 wants a 1538-pixel window, which does not
  fit every display. The app says so rather than silently clamping.
- Tk's drawing model has no gradients and no rounded rectangle, so depth is
  faked with layered strokes and stipple. It looks like a device; it is not a
  render.

## Alternatives rejected

**Run the GUI inside the container over X11.** Needs an X server on Windows and
makes a one-command app into a setup guide.

**Move QEMU to the host so everything is native.** Would need a Windows build of
the fork including our bridge, and CPython has no `AF_UNIX` on Windows anyway,
so the bridge would need a TCP transport regardless.

**Add TCP to the bridge and run the models host-side with the GUI.** The right
answer once input is needed, and premature for a viewer: it would trade a file
read for a protocol change, in service of nothing this app currently does.

**Ship a photograph of the device as the skin.** Better looking, and it breaks
the no-binary-assets rule and fixes the resolution.
