# ADR 0115 — The guest's clock does not follow the host's

**Status:** Accepted — 2026-10-02. Answers what ADR 0113 decision 7 left
open: a timed air is now the same run every time. Adds two carried QEMU
patches (ADR 0017).

## Context

ADR 0113 put boards on one air on one guest clock and fixed three places
where the host's timing reached the guest. It still found that two runs of
the same boards drifted apart under host load: a few hundred guest
instructions at first, then milliseconds by the first transmission. The cause
was "somewhere in QEMU's execution of the two cores".

### How it was found

Temporary traces wrote one line per event, stamped with QEMU's virtual time:

- every bridged register read and its value;
- every SPI and I2C reply, as a checksum;
- every change on a CPU interrupt line;
- every flash command and cache page load;
- every round-robin slice;
- every kick or interrupt arriving from outside the vCPU thread.

Six runs of one board went at once, so the host was busy, and the first line
where two runs differed was read in context.

### What was found: three defects

All three are in QEMU, not in this project's devices.

1. **The clock's jump over idle time is computed off the vCPU thread.**
   Under `-icount sleep=off`, a guest whose cores are all idle has its
   virtual clock jumped to the next deadline (`icount_start_warp_timer`).
   - The main loop calls that on every pass, and it checks that every vCPU
     is halted.
   - A vCPU sets `halted` in `helper_waiti` without the global lock, before
     the round-robin thread has accounted the instructions it just ran
     (`icount_process_data`).
   - From the main loop, the clock it reads does not include that running
     vCPU's count. `icount_get_raw_locked` accounts only `current_cpu`,
     which is NULL there.
   - So a jump taken in that window starts from a clock that is behind, and
     overshoots its deadline by however many instructions were missing, up
     to a whole slice.

   Seen as a run waking a whole tick (1 ms) early, and as a stretch with no
   I/O taking 54,000 instructions more in one run than in another.

2. **A vCPU's budget is cut at realtime deadlines.** `icount_get_limit`
   includes the nearest `QEMU_CLOCK_REALTIME` deadline ("they help with
   input processing"), which is a host time.
   - Where a budget ends is where the round robin hands over to the other
     CPU.
   - Two cores spinning on a lock they share spin for as long as the
     handover lets them.

3. **The SPI flash writes its backing store from memory the guest is still
   changing.**
   - `m25p80` keeps a working copy of the flash, and after a program or an
     erase it writes the changed range to the backing file with
     `blk_aio_pwritev`.
   - That write points straight into the working copy, and it runs whenever
     the host's I/O thread gets to it. It writes whatever the copy holds by
     then, including what the guest changed after the write was issued.
   - `esp32s3_cache` reads that backing file, not the working copy, whenever
     the MMU maps a 64 KB flash page.
   - So the cache could see flash contents that were too old, or a mixture,
     depending on the host.

   Found as the last residual. One core sometimes stopped part-way through
   a round-robin slice, with no traced I/O and no interrupt line moving. Its
   own code had taken a different path, because memory it read differed.
   - Waiting in the cache for writes in flight (`blk_drain`) made this rarer
     but did not end it. It cannot change what a write contains.
   - Writing the backing store synchronously did end it.

QEMU master still has all three. Neither the Espressif fork's issues nor its
pull requests mention any of them.

## Decision

1. **Without sleep, only the vCPU thread jumps the clock.**
   - `icount_start_warp_timer` returns at once when `sleep=off` and it is not
     on the vCPU thread.
   - The round-robin loop makes the jump itself when every vCPU is idle,
     after accounting their counts. It then goes on to run whatever is due
     instead of waiting for the main loop.
2. **Without sleep, budgets ignore realtime deadlines.** A realtime timer
   still runs; it just no longer decides where a vCPU stops.
3. **The SPI flash writes its backing store synchronously** (`blk_pwrite`),
   so the file holds what the chip held at a moment the guest decided. The
   cache's reads of it are then both current and the same in every run. Page
   programs and erases are short, and they are rare next to everything else
   a guest does.
4. **What a board knows of the air at a barrier is fixed by the barrier,
   not by when frames arrived.** This leak was in ADR 0113's own design.
   - At barrier k, a board waits for its peer's mark and then drains its
     socket.
   - By then the peer may already have run on into the next quantum and
     sent a frame from it. Whether that frame is known at barrier k or k+1
     depended on the host's scheduling.
   - A CAD in between would see it in one run and not in another, and
     Meshtastic then backed off by a different random slot.
   - Now a frame counts from the first barrier after its start. Every such
     frame was sent before its sender's mark, so it is surely in hand. Later
     frames wait.
   - Known frames are taken in an order of their own: start, sender,
     contents.

5. **Carried as two patches, recorded in `tools/build_qemu.sh`:**
   - `icount-sleep-off-deterministic.patch`: decisions 1 and 2, in QEMU
     core;
   - `flash-writes-synchronously.patch`: decision 3, in `hw/block/m25p80.c`,
     which is QEMU core too. It is the Espressif cache's way of reading the
     backing file that exposes it.

   They are offered upstream when the user approves, as ADR 0017 asks. QEMU
   master has the same code for both.

## Consequences

**Measured.** One timed Meshtastic board, six runs at once, compared through
about 10.65 s of guest time. That is roughly 800,000 traced events per run:
bridged reads, SPI and I2C replies, CPU interrupt lines, round-robin slices.

- **Before:** most batches had runs diverging within 6 s, for example 3 of 4.
- **With decisions 1 and 2:** about one run in twelve still diverged, always
  at the same place, where the guest programs flash.
- **With all three:** 24 of 24 runs, over four batches, are identical event
  for event and slice for slice.
- **Two meshes at once** (`logs/timed_mesh.py`): identical, all ten packets
  sent and heard over 75 s of guest time, to the microsecond.
  `tests/test_mesh_timed.py` now checks exactly this.

**Good**

- A timed air is the same run every time, under load: ADR 0113's aim. A
  mesh failure can be replayed by running it again.
- A guest that reads, through the cache, flash it has just written gets the
  new contents, not old ones or a mixture.

**Costs, accepted**

- Two more carried patches, both in QEMU core.
- Flash programs and erases now wait for the host's disk. They are rare and
  short next to everything else a guest does.
- Determinism holds for what a run is fed from inside. Input from the host
  while a run goes, such as a touch or a key, still lands at whatever guest
  instant the host delivers it.
