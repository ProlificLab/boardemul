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
