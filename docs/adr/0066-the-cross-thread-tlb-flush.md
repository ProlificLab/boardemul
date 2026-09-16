# ADR 0066 — The cross-thread TLB flush

**Status:** Accepted — 2026-09-16. Fixes the defect ADR 0041 worked around.
ADR 0041's decision is not reversed here: one translation thread stays the
default, for its own reasons.

## Context

ADR 0041 characterised a crash and could not fix it. What it established:
QEMU dies with a segmentation fault under `BOARDEMUL_TCG_THREADS=multi`, under
load; the fault is inside generated code at a *guest* address in internal RAM,
which means the translation entry it used had a zero host offset -- it was read
while it held nothing. Two candidates were ruled out by measurement. What was
left, in that page's own words, was "the address-translation path itself, under
two threads".

One correction to that page before anything else. It puts the rate at "about
one run in fifteen", and re-measuring it here does not support that: two faults
in two hundred and twenty-four runs, nearer one in a hundred. Nothing follows
from the difference except that a claim of "fixed" needs more runs than ADR
0041's number implies, which is what the amplifier below is for.

It is exactly that, and it is five lines of upstream QEMU.

**`tlb_flush()` is not a request for a flush; it is the flush.** In this
version, `accel/tcg/cputlb.c`:

```c
void tlb_flush_by_mmuidx(CPUState *cpu, uint16_t idxmap)
{
    assert_cpu_is_self(cpu);
    tlb_flush_by_mmuidx_async_work(cpu, RUN_ON_CPU_HOST_INT(idxmap));
}
```

Despite the name of the callee, the work runs **on the calling thread**,
against `cpu`'s table. It is correct only when the caller is that CPU's own
thread, and the assertion that would say so is compiled out:
`assert_cpu_is_self` expands to nothing unless `DEBUG_TLB_GATE` is set, and
`DEBUG_TLB` is commented out at the top of the file. A cross-thread call is
therefore not an error at build time, at run time, or in any build anyone
normally makes.

**The fork's cache model makes exactly that call.** `hw/misc/esp32s3_cache.c`
models the flash and PSRAM cache as an IOMMU region, so every time the guest
rewrites an MMU entry the model notifies its IOMMU, and upstream's TCG hook
for that is `system/physmem.c`:

```c
static void tcg_iommu_unmap_notify(IOMMUNotifier *n, IOMMUTLBEntry *iotlb)
{
    ...
    tlb_flush(notifier->cpu);
    notifier->active = false;
}
```

There is one such notifier **per CPU**, and `memory_region_notify_iommu` walks
all of them on whichever thread performed the register write. With one TCG
thread per vCPU, core zero writing an MMU entry runs the notifier belonging to
core one and flushes core one's table from core zero's thread -- clearing it,
under a lock the reader does not take, while core one is executing generated
code that has already loaded an entry out of it.

**Measured, not deduced.** A diagnostic in that function, reporting when
`notifier->cpu` is not the calling thread, is kept in
`qemu-patch/experiments/report-cross-thread-tlb-flush.patch`. One ordinary
Meshtastic boot on two threads does it **three** times, against twenty-three
legitimate self-flushes:

    === iommu notify: thread of cpu 0 flushing cpu 1 (cross 1, self 20)
    === iommu notify: thread of cpu 0 flushing cpu 1 (cross 2, self 22)
    === iommu notify: thread of cpu 0 flushing cpu 1 (cross 3, self 23)

So it is not a rare path. It is a frequent path with a narrow window, which is
the shape this crash always had.

**Not every wrong-thread flush is this bug**, which is worth stating because
assuming otherwise cost a wrong conclusion here. The machine's own
`cpu_reset()` in `esp32s3_machine_init` also flushes from the main thread, and
by then the vCPU threads exist -- so `cpu->created` does not distinguish it --
but none of them has begun executing guest code, and there is nothing to race
with. It happens once per boot and is harmless -- and it is the line the
amplifier below prints even with this patch applied, so that line is not a
sign of anything. The two are told apart by their backtrace and nothing else:
a variant of the amplifier that dumped one is how this was identified, and
`qemu-patch/experiments/README.md` says to add it back rather than guess if a
third arrival ever turns up.

Three things this account explains that the earlier theories did not:

- **Why the faulting address is in internal RAM.** `tlb_flush` clears *every*
  index, not the page that moved. The region that faults is one that is never
  remapped; it is wiped anyway, which is why ADR 0041's counter-evidence
  pointed away from the cache and the cache was still involved.
- **Why one translation thread is immune.** Both vCPUs share that thread, so
  `qemu_cpu_is_self` is always true and every flush is local. Nothing about
  ADR 0041's workaround was coincidental.
- **Why it needs load and never fires under a debugger.** The window is the
  length of a `memset`, and a debugger moves both threads out of it.

## Decision

**Hand the flush to the CPU that owns the table.** `async_run_on_cpu` when the
notifier's CPU is not the calling thread; the existing direct call when it is.

This is the pattern the same subsystem already uses for cross-CPU TLB
maintenance -- `flush_all_helper` in `accel/tcg/cputlb.c` queues
`async_run_on_cpu` for every CPU but the caller -- so it introduces no new
mechanism, only the missing use of one. `async_run_on_cpu` takes the target's
own work mutex rather than the big lock, so it is safe from a device write on
any thread, and it kicks the target out of its translation block, so the flush
lands in microseconds rather than whenever the guest next traps. A halted CPU
still runs it: `qemu_wait_io_event` drains the work list, so a core sitting in
a wait instruction does not sit on a stale table.

The queued function takes only the CPU, not the notifier. That is deliberate
rather than merely smaller: notifiers are freed in `tcg_exec_unrealizefn`, so
work that dereferenced one could in principle outlive it, and work that does
not cannot.

**The default configuration cannot be affected by this patch.** In round-robin
mode every CPU's `thread` is the one shared TCG thread
(`tcg-accel-ops-rr.c` assigns `single_tcg_cpu_thread` to each), so
`qemu_cpu_is_self` is always true and the direct call is always the one taken.
The new branch is unreachable with one translation thread, which is what makes
this safe to carry while ADR 0041's default stands.

It is a **carried patch** under ADR 0017: it changes QEMU's own logic rather
than adding to it, so it is a declared diff in `qemu-patch/`, and it is deleted
when it lands upstream rather than carried on.

It is **not yet reported**. The crash already has an upstream issue --
<https://github.com/espressif/qemu/issues/175>, filed by ADR 0041 -- and this
page is the answer to it, but posting the diagnosis and offering the diff is a
separate, outward-facing step and is not done here.

## Consequences

**Good**

- **The crash is gone.** `tools/tlb_crash_rate.sh` is the count, so this is
  repeatable rather than a recollection. First the plain build, on ADR 0041's
  own reproduction -- concurrent Meshtastic boots on two translation threads,
  each with its own build directory:

  | concurrency | runs | faults |
  | --- | --- | --- |
  | 14 | 56 | 1 |
  | 28 | 168 | 1 |

  About one boot in a hundred. **That rate cannot demonstrate a fix**, which is
  the awkward part of this page: a zero over the same number of runs would come
  up by chance about a third of the time. So the window was widened on purpose
  -- `qemu-patch/experiments/amplify-cross-thread-tlb-flush.patch` repeats a
  wrong-thread flush with a pause between passes -- and the two builds compared
  at the same setting, fourteen concurrent, eighty-four runs each:

  | build | runs | faults |
  | --- | --- | --- |
  | amplified, no fix | 84 | 5 |
  | amplified, this patch | 84 | 0 |

  At the first arm's rate, a zero in the second would happen by chance about
  once in two hundred times. The two arms differ only in this patch; the
  amplifier is identical in both.

  Two honest limits on that. The baseline was re-measured rather than quoted
  from ADR 0041, and it disagrees with it, as noted above. And the amplifier
  turns out to work partly by perturbing start-up rather than purely by
  widening the window -- guarding its condition with `cpu->created`, which is
  the correct way to read `cpu->thread`, removes the amplification altogether.
  That negative result is recorded beside the patch. It does not affect the
  comparison, which used one amplifier in both arms, but it does mean the
  amplifier is a blunt instrument rather than a clean one.

- **ADR 0041's open question is closed**, and its suspicion was right: the
  defect is QEMU's own machinery, not this project's devices.

- **`thread=multi` becomes available**, which the WPA2 handshake needs. ADR
  0064 found that the guest's spinlock stall after associating does not happen
  on two threads, so the way through that wall was always this page.

**Costs, accepted**

- **A flush that was synchronous is now queued.** Between the register write
  and the target CPU running the work, that CPU can still use a mapping the
  guest has just replaced. The window is one translation block, shortened
  further by the kick, and upstream's own cross-CPU flushes accept the same
  window. The alternative that closes it is rejected below.

- **A second race in the same five lines is left alone.** `notifier->active`
  is written by the notifying thread and read and written by the target's,
  with no synchronisation; losing that store can skip a later flush and leave
  a stale mapping. It is visible by reading and has not been seen to
  misbehave, and `qemu-patch/experiments/README.md` already states the rule
  for that class of thing -- a hazard nobody has watched fail is a weaker
  report than one that has. It is written down here so the next reader does
  not have to find it again.

- **This does not change the default.** ADR 0041 chose one thread partly to
  dodge this crash and partly because it measured *faster* on this workload,
  every peripheral being serialised behind one bus socket anyway. Only the
  first reason is gone. Flipping the default would re-time every test in a
  suite whose WiFi half is sensitive enough to notice which filesystem it
  writes to (ADR 0063), so `multi` stays something a run asks for.

## Alternatives rejected

**`async_safe_run_on_cpu`.** Brings every vCPU to a halt point before the work
runs, which closes the stale-mapping window completely. It also makes a global
synchronisation out of every MMU entry write, and ESP-IDF's flash mmap writes
them in a loop, one per 64 KB page. That is a cost on a path the guest uses on
every file read, in exchange for closing a window nothing has been observed to
fall into.

**Turn the assertion on and keep one thread.** Defining `DEBUG_TLB` would have
caught this at the first cross-thread call, and it is worth knowing that the
check exists and is off. But it reports the bug rather than fixing it, and it
brings a great deal of unrelated logging with it.

**Stop modelling the cache as an IOMMU.** The IOMMU region is how the fork
gets a flash window whose mapping the guest can change, and it is the fork's
design, not a defect. Replacing it to avoid one bad flush in upstream's hook
would be a far larger patch to carry than the hook itself.
