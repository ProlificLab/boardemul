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
