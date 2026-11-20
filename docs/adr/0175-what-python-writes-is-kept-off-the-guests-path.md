# ADR 0175 — What the run's Python side writes is kept off the guest's path

**Status:** Accepted — 2026-11-20. Follows ADR 0174 to the Python side.

## Context

ADR 0174 moved what QEMU writes off the folder shared from the host. What
the Python side writes into the run's folder was left where it was. After
it, a Meshtastic boot to "Config Send Complete" still took 2 to 6 s longer
with its output on the shared mount than on local disk (four pairs,
2026-11-20, `logs/dbg/out_ab.sh`).

Most of what Python writes is small and on threads the guest never waits
for: at its normal level the bus log holds tens of lines a run. Two writes
are neither.

1. **The panel's frames.** Both panel models (`St7789Panel`, `EPaperPanel`)
   render on the thread that serves the SPI bus, and render encoded the
   frame as a PNG twice, at zlib's level 9, once for `frame-NNN.png` and once
   for `latest.png`, then wrote both. Every duplex exchange on that bus --
   the SD card's reads, the radio's commands -- waits for the thread, and so
   does the guest that made it. Measured in the build container for a
   320 × 240 frame of the kind Meshtastic draws:

   | | per frame |
   |---|---|
   | one encode | 10.9 ms |
   | both files written, folder shared over 9P | 29.6 ms |
   | both files written, local disk | 0.4 ms |

   About 51 ms of the bus's time a frame when the folder is shared, 22 ms
   when it is not. Meshtastic's WiFi-screen run drew 167 frames.

2. **The OTG port's log** (ADR 0166). The cable's thread, the one QEMU's USB
   controller waits on, appended what the device sent unbuffered, a USB
   packet at a time, straight into the run's folder. On Meshtastic that port
   carries the firmware's whole log: some 22 KB in a boot, hundreds of
   writes, each a 9P round trip.

## Decision

1. **A board gives its panel a `FrameWriter`** (`emu/png.py`): a thread of
   its own and a queue. Render hands it the frame's pixels and the paths it
   goes to; the writer encodes once and writes each path, beside its name
   and renamed onto it as before, in the order the frames came. The panel's
   `flush()` and the board's `stop()` drain it, so the last frames are on
   disk before a run reports or says it stopped. A panel built without a
   board writes at once on the caller's thread, as before; the panels' unit
   tests read `latest` straight after a render.

2. **The OTG port's log grows on local disk where the run's folder is
   shared**, and is copied there every half second and when the cable
   closes: ADR 0174's `Mirror`, owned by the cable. A serial bridge on that
   port (`--serial-tcp PORT:otg`) follows the local file, so a client sees
   no copying delay.

3. **The bus log stays as it is.** At INFO it is too small to matter; a
   verbose log is a debugging run's, and its cost is accepted there.

## Measured

After both, on 2026-11-20, five boots each way in alternating order
(`logs/dbg/start_ab3.sh`): shared 23.6, 16.7, 21.5, 19.5, 19.5 s; local
19.4, 18.1, 23.3, 14.6, 19.8 s. Means 20.2 and 19.0 s against a spread of
nine seconds: no difference the runs can show. QEMU starts 1.5 to 2 s after
`run.py` either way.

Sampling every thread's kernel wait during a shared boot
(`logs/dbg/wchan_boot.sh`, which names Python's threads through
`logs/dbg/named_run.py`) leaves, in 9P, the input file's poller -- its own
thread, which the guest does not wait for -- and QEMU's reads and writes of
the device folder's flash, which a local output folder does not change.

## Consequences

**Good**

- A run's speed no longer depends on where its folder is.
- The SPI bus no longer waits on zlib or the host's filesystem; each frame is
  encoded once rather than twice, in zlib, which releases the interpreter's
  lock.

**Costs, accepted**

- `panel.latest` names a file a moment before it is on disk during a run.
  Everything that reads frames during a run reads `latest.png` from disk,
  which is still replaced whole.
- A guest that draws faster than the host encodes queues frames in memory,
  230 KB each. The colour panel renders at most every half second, the
  e-paper panel once per refresh.
- A reader of the OTG log in the run's folder, rather than through the
  bridge, sees it up to half a second late, as the UART's since ADR 0174.
- A boot's own spread, nine seconds in ten runs on one host, is larger than
  anything left to gain here, and not explained by this ADR.
