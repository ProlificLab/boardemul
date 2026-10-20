# ADR 0134 — The window starts a device

**Status:** Accepted — 2026-10-19.

## Context

ADR 0133 made a device a folder and `--device` a way to run one, but using
it is still two terminals: start a run in one, open the viewer in another,
pointing it at the run's frames by hand. The ordinary case this project set
out for — pick a device, start it, click it — is three steps of plumbing
away.

The window is a viewer bound to a frame directory by flags, and deliberately
knows nothing about runs. That has served the tests well: a viewer that
attaches to anything catches up on the next poll, runs come and go around
it.

## Decision

1. **An explicit `--frames` keeps today's behaviour exactly.** Scripts,
   tests and CI are untouched. The picker appears only when the window is
   opened without a frame directory to watch.

2. **Without one, the window first offers the devices it can start**: the
   folders `emu/device.py` knows about, a form for making a new one (name,
   board, firmware — the folder's own `create`), and "watch a folder…",
   which is today's behaviour pointed wherever the user says.

3. **Starting a device is one subprocess of `emu/run.py --device`**, with
   `--bus` and an output directory under `logs/devices/<name>/`, and a
   timeout of a day, because a window-run device stops when its user stops
   it, not when a ceiling does. No third way to start a run is invented:
   the chooser orchestrates through the folder format and the emulator's own
   flags, and `tools/device.py` stays the command-line for folders.

4. **A run the window started, the window can stop — and must.** Stopping is
   the child's own interrupt, so its shutdown path runs and the card goes
   back to the folder (ADR 0133's persistence depends on that path). The
   window shows a stop control while its run is alive, and stopping the
   window stops a run it started. A window that only watches stops nothing.

## Consequences

**Good**

- The ordinary case is one gesture: open the window, pick a device, click
  it. Everything underneath is the same flags a script would pass.
- Persistence gains its front door: configure the dialog once from the
  window, close it, open it again, and the node is still itself.

**Costs, accepted**

- The window now owns a child process, which a pure viewer never did. It is
  one subprocess with one interrupt path, and it is the same one the slow
  tests already drive.
- The chooser reads the devices folder once, at the top; a folder created by
  another tool while the chooser sits open appears on the next opening.
