# ADR 0011 — Carry transactions, not register accesses; scope the C tier to SoC silicon

**Status:** Accepted — 2026-08-25
**Supersedes:** the boundary rule in [ADR 0001](0001-qemu-fork-as-cpu-engine.md)
(decisions 2 and 3) and the transport reasoning in
[ADR 0005](0005-bus-bridge.md) (decisions 5 and 6)

## Context

ADR 0001 drew the boundary at "no device intelligence in C". That rule produced
a working emulator for one board with roughly ten minutes of C and a day of
Python device modelling, and it was the right call for getting here. It also
has a defect that only became visible once something had to move real data
through it.

The bridge carries **one round trip per MMIO register access**. Measured on the
factory firmware driving its e-paper panel:

```
bytes per frame        :  19,200      (two data planes, 240x320 at 1bpp)
round trips per byte   :  12.1        (the guest's own read-modify-write pattern)
round trips per frame  : 232,320
round-trip floor       :  ~60 us      (Docker/WSL2; 54 us in plain WSL2)
observed frame time    :  ~34 s
```

The consequence is not merely slowness. LVGL only samples its input device
between full-screen refreshes, so at 34 seconds per refresh a touch has to be
held across a refresh boundary to be seen at all. Measured: a 10-second hold
produced 4 touch reports; a 100-second hold produced 470. But a hold long
enough to be sampled is a ten-second *guest* long-press. Those two constraints
cannot both be satisfied, which is why input works at every layer and still
cannot land a click (ADR 0010, postscript).

Two fixes were considered and one was very nearly taken.

**Shared memory with spin-waiting** would have cut the round trip from 60 µs to
about 1 µs — roughly 170x, enough to make the device usable. It was the
recommendation until the arithmetic was examined from the other end: it makes
232,320 round trips fast rather than making them unnecessary, at the cost of a
permanently spinning core and a substantially more complex transport. It
optimises the wrong axis.

**The number itself is the defect.** GxEPD2 holds chip-select low for the whole
data phase of a frame, so a single CS window carries 9,600 bytes. A bridge that
carried SPI *transactions* rather than register accesses would need on the order
of fifteen round trips per frame:

| boundary | round trips/frame | frame time at 60 µs |
|---|---|---|
| register access (today) | 232,320 | ~34 s |
| SPI transaction (per CS window) | ~15 | ~1 ms |

Four orders of magnitude, on the plain Unix socket already in place.

There is a second reason to revisit the rule now. This project is a strong
candidate for covering the wider LilyGO range, and the register-level boundary
scales worse as boards get faster: a DMA-driven LCD moves 153 KB per frame at
30 fps, which no per-access design survives.

## Decision

1. **The bridge carries transactions, not register accesses.** A generic SPI
   controller buffers bytes between chip-select assert and deassert and ships
   the completed transaction once. An I2C controller ships a completed command
   list. The unit crossing the boundary becomes the unit the *bus* defines, not
   the unit the CPU happens to use.

2. **The C tier is scoped to SoC silicon, and the boundary rule is restated
   accordingly.**

   > **No board-specific knowledge in C.**

   This replaces "no device intelligence in C". A generic SPI controller that
   buffers a CS window is not device knowledge — it is what SPI is. A model
   that knows `0x13` selects the new-image plane on a UC825x is, and stays in
   Python.

3. **The C tier covers, per SoC:** SPI (GP-SPI2/3), I2C, GPIO including the pin
   matrix and interrupt path, I2S, LCD_CAM, and GDMA. These are identical across
   every board carrying that chip, change rarely, and are precisely what
   Espressif may upstream one day — at which point ours are deleted rather than
   maintained.

4. **Everything board-specific stays in Python:** panels, PMICs, radios, touch
   controllers, keyboard controllers, IMUs, codecs. This is where the work
   actually is, and it keeps the property that made the project productive — a
   wrong assumption is a one-line edit and a re-run, with no rebuild.

5. **The transport stays a plain Unix socket.** Once granularity is fixed the
   round-trip rate falls to hundreds per second, where 60 µs is irrelevant.
   Shared memory is explicitly rejected rather than deferred.

6. **Writes become fire-and-forget.** ADR 0005 decision 6 made every write wait
   for an acknowledgement, for ordering. That is stricter than necessary: the
   socket is already FIFO, so a read issued after a write still observes it. Not
   blocking on writes is close to a 2x improvement on any transport, for about
   ten lines. The cost is that a model crash loses writes silently instead of
   stalling visibly, which is a worse failure mode and is accepted knowingly.

## Consequences

**Good**

- Display-driven firmware becomes usable rather than merely correct, and with it
  every interaction that depends on the UI loop actually running.
- The design stops degrading as boards get faster. A DMA-driven LCD is *cheaper*
  across this boundary than the e-paper is today, because the framebuffer never
  leaves QEMU — the bridge sees descriptor writes and a completion interrupt.
- A new board becomes a board description plus Python device models, many of
  them already written. The AXP2101 appears across half the LilyGO range, the
  SX1262 on most of the LoRa boards, the ST7789 on the LCD ones.

**Costs, accepted**

- **This is a real reversal, and it is expensive.** The C footprint goes from one
  file to roughly five per SoC. That is C we own, against a QEMU fork that moves
  underneath us, and every tag bump now has surface area it did not have before.
  ADR 0001's promise that an upgrade is a two-line diff no longer holds.
- Three SoCs eventually need the tier written: ESP32 (LX6), ESP32-S3 (LX7) and
  ESP32-C3 (RISC-V). Same controllers, different machine.
- A generic controller in C is a place bugs can hide from the fast iteration
  loop. The mitigation is that these models are small, well-specified by the
  TRM, and shared by every board — so they get exercised constantly.
- **Python needs a path to guest memory** for DMA-driven peripherals, which the
  MMIO bridge does not provide. Either a bulk-read call in the bridge (still
  only moving bytes) or letting QEMU's own GDMA deliver a completed buffer.
  Unresolved, and the first thing to design when an LCD board is attempted.

**What does not change**

- The Python device layer, the file-based frame and input channels, the ADR
  discipline, and the habit that produced every real finding in this project:
  measure before deciding.

## Alternatives rejected

**Shared memory with a spin-wait transport.** Would deliver roughly 170x and was
the standing recommendation before the arithmetic above. Rejected because it
accelerates round trips that should not exist, permanently occupies a core, and
locks in the register-level boundary that is the actual defect.

**Leave the boundary where it is and accept the speed.** Defensible for this one
board — e-paper refreshes in 1.1 s on real hardware, so even 0.2 s would be
faster than the device. Rejected because it forecloses the rest of the range,
and because it leaves interaction permanently unusable.

**Move all peripherals into C.** Fastest possible, and it discards the property
that made this project work. Board-specific device models are where the
uncertainty lives, and they need to be editable in seconds.

**Adopt Renode instead.** Solves multi-architecture and device modelling out of
the box. Rejected in ADR 0001 for the same reason it is rejected now: the
framework would own the structure, and understanding every line is the point.
