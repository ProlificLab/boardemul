# Experiments

Candidate changes to QEMU that are **not applied** unless asked for by name:

```sh
BOARDEMUL_QEMU_EXPERIMENTS=cache-declare-direct-writes tools/build_qemu.sh
```

The build says which are active, and fails if a named one does not apply.

## Why this exists

A patch one directory up is a fix: a defect was found, diagnosed, reported,
and the diff is carried until it lands upstream (ADR 0017). This directory is
for the step before that — a change that might be a fix, built and measured
against a real firmware to find out.

Without somewhere to put them, those attempts live in a working tree and are
reverted, and the next person to have the same idea has no way of knowing it
was already tried. A negative result is worth keeping precisely because it
stops the same ground being covered twice.

An experiment is not a fix and must never quietly become one. Nothing here is
applied by default, nothing here is upstreamed, and anything that turns out to
be right graduates: it moves up a directory, gets an ADR, an issue and a pull
request, and stops being an experiment.

## What is here

### `report-cross-thread-tlb-flush.patch` — a diagnostic, and it found the bug

Prints a line when `tcg_iommu_unmap_notify` is about to flush the TLB of a CPU
that is not the calling thread, which `tlb_flush()` does synchronously and
therefore unsafely. It is here rather than one directory up because it reports
rather than fixes; the fix it led to is `../tlb-flush-owning-thread.patch`
(ADR 0066).

Kept because the measurement is worth repeating after any tag bump: one
Meshtastic boot on two threads should print nothing once the fix is in, and
three lines without it.

    BOARDEMUL_QEMU_EXPERIMENTS=report-cross-thread-tlb-flush tools/build_qemu.sh

### `report-watchpoint-hits.patch` — a diagnostic, and it cleared QEMU

Written to find out whether "Stack canary watchpoint triggered (ipc1)", a
panic both vendor builds and Meshtastic showed under load, was a false trigger
in QEMU's Xtensa data breakpoints. It was not, and this is how that was shown.

Three things, all in `target/xtensa/` and all logged under `-d unimp`, which
every run already has:

- **Every hit** (`wp-hit:`): the access address, every DBREAK register and the
  QEMU watchpoint each became, the core's last eight watchpoint moves with the
  stack pointer each was made from, and 2 KB of the stack above the hit.
- **A near-miss watch** (`wp-near:`): a second watchpoint, invisible to the
  guest, over the lowest 384 bytes of whichever stack DBREAK1 is guarding. It
  goes through the `debug_check_watchpoint` hook, which this patch adds, and
  that hook says no, so the guest never sees it. Each time a stack's lowest
  store goes deeper, one line is logged. A store within 96 bytes of the canary
  is logged with all 64 physical registers and the stack.
- **The decision** (`cause`, `cintlevel`, `debug level`) the breakpoint
  handler then made.

What it found, over 79 loaded boots of `T-Deck_UnitTest_250620`, four of which
panicked:

- Every hit was real. The watch covered `0x3fcf1380+32` and the store that
  fired was at `0x3fcf138c`, inside it.
- On every boot, ipc1 runs the GPIO ISR-service install
  (`gpio_isr_register_on_core_static` → `esp_intr_alloc` →
  `heap_caps_malloc` → `multi_heap`). Its deepest store is then 216 bytes
  from the end of its 1024-byte stack, which ends at `0x3fcf1374`.
- The panic is a core-1 tick that is pending when that path's heap lock ends.
  The saved PS shows interrupt level 0, at the ROM's interrupt-level restore,
  with SP 300 bytes from the end. Every core-1 tick ends in a context switch:
  in an ISR on core 1 this firmware's `xTaskIncrementTick` returns true without
  looking (`0x403807ea`). The tick pushes a 192-byte frame, and the
  context switch then runs `vTaskSwitchContext` (48) and its kernel-lock
  acquire (64) below that frame, on ipc1's own stack. That is 304 bytes, which
  ends 4 bytes past the stack. The near-miss log shows interrupts landing in
  that path in 9 of 21 boots, and in two of them at the one point that
  overflows.

Meshtastic 2.7.10 measured the same way over eight boots shows the same: one
panic, at the same frame, through its own copy of the same IPC function.

So the overflow is the firmware's. The emulator only changes how often a tick
lands there: on the wall clock a loaded guest gets far fewer instructions per
tick than silicon does (ADR 0027). What the emulator did wrong was refuse the
reboot that follows, and ADR 0099 is about that.

Kept because the next canary panic should start from this, not from
suspecting QEMU again:

    BOARDEMUL_QEMU_EXPERIMENTS=report-watchpoint-hits tools/build_qemu.sh

### `report-cpu-resets.patch` — a diagnostic, and it found two defects

Written to find out why one reboot after the ipc1 canary printed the ROM's
banner twice (ADR 0114). Three kinds of line, all under `-d unimp`:

- `cpu-reset: PROCPU requested by core 1 at … ns; core0 pc …, core1 pc …`:
  each CPU reset the guest writes to RTC_CNTL, who wrote it, and where both
  cores were. `(halted)` marks a core that was not running.
- `cpu-stall: core 0 stalled by core 1 at … ns`: each change of a stall.
  The firmware's read-modify-writes pass through an unstalled value, so a
  stall that is already on shows as four quick lines.
- `cpu-reset: machine reset at … ns resets core0`: each reset QEMU then made
  of the machine, and which cores it reset.

The requests and stalls are logged in `hw/misc/esp32s3_rtc_cntl.c`, where the
guest writes them, so the patch also applies on top of
`../core-reset-and-stall.patch` and shows the fix's timeline.

What it found, driven by `tools/restart_probe` (one restart from each core, by
`esp_restart()` and by `abort()`): ESP-IDF's restart from core 1 resets core 0
and stalls it, then resets it again and releases it. The stall did nothing,
and each reset was a machine reset after which core 0 ran first for a whole
time slice. The stall arrived 106 ms after the first reset, with core 0 deep
in its ROM, and the last reset found core 0 in the bootloader. ADR 0123 has
the numbers and the fix.

    BOARDEMUL_QEMU_EXPERIMENTS=report-cpu-resets tools/build_qemu.sh

### `amplify-cross-thread-tlb-flush.patch` — makes a 1% race a visible one

`accel/tcg/cputlb.c`, in `tlb_flush_by_mmuidx_async_work`: if this flush is
running on a thread that does not own the table, clear the table twenty
thousand times over with a twenty-microsecond pause between passes, so the
owning CPU can refill an entry and then be caught between reading its
comparator and reading its addend.

It exists because the crash in ADR 0041 happens on about one boot in a hundred,
and a difference that size takes hundreds of runs to see. With this, the rate
rises by more than an order of magnitude, which is what let ADR 0066's fix be
demonstrated in a few dozen runs instead of a few thousand.

Measured with it, fourteen concurrent boots, eighty-four runs each arm:

| build | runs | faults |
| --- | --- | --- |
| amplified, no fix | 84 | 5 |
| amplified, with `../tlb-flush-owning-thread.patch` | 84 | 0 |

**It prints one line per boot even with the fix applied, and that is expected.**
A wrong-thread arrival here is not by itself the defect. Two are known, and
mistaking one for the other cost a wrong conclusion here once:

- upstream's IOMMU hook, flushing a *running* CPU's table -- the defect;
- the machine's own `cpu_reset()` in `esp32s3_machine_init`, on the main
  thread. The vCPU threads exist by then but none has begun executing guest
  code, so there is nothing to race with. This is the line you see.

They are told apart only by their backtrace. A variant of this patch that
printed one -- `backtrace_symbols_fd` on the first hit -- is what identified
the second, and it is worth adding again rather than guessing if a third ever
appears.

### The `cpu->created` guard: tried, and it stops the amplifier working

`cpu->thread` is documented as live only once `cpu->created` is set, so reading
it before that is wrong and the obvious tidy-up is to guard the condition with
it. Doing so **removes the amplification entirely**: eighty-four runs with the
fix reversed and the guard added gave *zero* faults, against five without it.

So most of this patch's effect is not the amplification it was written to do.
It comes from also catching the main thread's flushes from before the vCPU
threads exist, which delays machine start-up by hundreds of milliseconds and
evidently changes how fourteen concurrent boots phase against each other. That
is a blunt instrument, and it is left ungated only because it is the form the
numbers above were taken with. Anyone tightening it must re-measure both arms.

Do not leave it applied. Each amplified arrival spends 400 ms with the big lock
held, so the guest's timers and its other core's device access stall with it.

### `cache-declare-direct-writes.patch` — tried, no effect on its target

`hw/misc/esp32s3_cache.c` writes guest RAM directly through
`memory_region_get_ram_ptr()` in two places: filling a page from flash with
`blk_pread()`, and stamping a discarded page with `0xdeadbeef`. Neither tells
QEMU the memory changed, so code already translated from those pages stays
live over contents that no longer exist.

This declares both writes with `memory_region_set_dirty()`.

**It was written to fix the crash in ADR 0041 and it does not.** Measured
against the reproduction there — fourteen concurrent boots, two translation
threads — the crash rate was three faults in fifty-six runs, which is what it
was without this. That is why it is here rather than one directory up.

The missing invalidation still looks like a real hazard on its own, and it may
yet matter to something other than that crash. It has not been reported
upstream, because a hazard nobody has seen misbehave is a weaker report than
the three already filed.
