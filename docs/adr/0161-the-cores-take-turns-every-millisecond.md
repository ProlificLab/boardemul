# ADR 0161 — The cores take turns every millisecond

**Status:** Accepted — 2026-11-19. Adds to ADR 0041, whose single translation
thread it keeps.

## Context

An operator's first question after "does it run" is "is it fast enough".
Meshtastic on the T-Deck reached its home screen about fifty seconds after
start, where a board takes a few. Without `-icount` the guest's clock is the
host's, so a firmware's own timestamps say how long each step took, and
`tools/speed.sh` reads them: when the firmware looks for its boot logo, and
when it reads its UI settings the second time, which is when the home
screen is up.

**Where the time goes.** QEMU's `hotblocks` plugin counts how often each
translated block runs (`tools/hot_profile.sh`). In 90 s of that boot, nine
emulated instructions in ten were in two loops, each one core waiting on the
other:

| Block | What it is | Runs in 90 s |
| --- | --- | --- |
| `0x403851a7`, 5 instructions | `spinlock_acquire`'s `S32C1I` retry, the lock held by core 0 | 532 million |
| `0x40377381`, 4 instructions | a flag poll after an IPC call to the other core | 78 million |

The next block ran five million times.

**Why.** ADR 0041 runs both cores on one host thread. QEMU's round-robin
then runs one core until it halts or until its turn ends, which is every
100 ms of virtual time (`TCG_KICK_PERIOD`). On silicon a core that waits on
the other waits as long as the other takes to answer, microseconds. Here the
other core is not running at all: the waiter spins out the rest of its
turn, and every handshake between the cores costs up to 100 ms.

**Two threads, again.** ADR 0041 measured one thread as fast or faster.
Measured again on this boot, two threads reached the home screen at 73 s
once and not within 90 s the other time.

## Decision

1. **A turn lasts 1 ms.** A device of ours, `esp-core-slice` (in
   `qemu-patch/esp_guest_clock.c`, with the other device about guest time),
   runs a virtual-clock timer every `period_us` and makes the running core
   leave its translated code, as QEMU's own kick does; the next core runs.
   `run.py` adds it to every run. `BOARDEMUL_CORE_SLICE_US` overrides the
   period, and 0 leaves QEMU's 100 ms.

2. **It does nothing where turns are not the problem**: under several
   translation threads, where the cores already run at once, and under
   `-icount`, whose budgets end each turn at the next timer deadline already
   and whose interleaving the deterministic runs depend on (ADR 0115).

3. **One translation thread stays the default** (ADR 0041).

4. **A failed `S32C1I` does not give up the turn.** It was built and
   measured, and is kept in `qemu-patch/experiments/` with its numbers: it
   removes the spinlock loop from the profile and makes the boot slower.

5. **`tools/speed.sh` and `tools/hot_profile.sh` are how this is measured
   again**, in the build container, after a QEMU pin moves or a device's
   timing changes.

## Measured

`tools/speed.sh`, 90 s per run, runs interleaved so the host's load falls on
every variant alike. Time to the home screen, guest milliseconds:

| Variant | Runs | Mean |
| --- | --- | --- |
| QEMU's 100 ms turn | 63.7, 51.3, 41.5, 54.7, 50.6 | 52.4 s |
| 1 ms turn | 53.8, 35.5, 44.3, 38.3, 34.6 | 41.3 s |
| 1 ms turn and the `S32C1I` yield | 60.9, 82.7, 45.1 | 62.9 s |
| the `S32C1I` yield alone | 70.2, 45.9, 45.4 | 53.8 s |
| two translation threads | 73.1, none in 90 s | — |

The boot logo moved alike: 16.6 s to 13.3 s on the means of five. The
spread between runs of one variant is as wide as the gain, which is why
there are five of each; four of the five 1 ms runs beat the mean of QEMU's
turn.

A 200 µs turn was tried too. In one run of two the firmware panicked on
ADR 0099's canary (ipc1's stack, overflowed in `spinlock_acquire` while a
tick forces a context switch) within 17 s. How often cross-core races land
depends on the turn, so the turn is the shortest measured without it.

## Consequences

**Good**

- The boot reaches its home screen about a fifth sooner.
- A handshake between the cores costs at most a millisecond instead of a
  tenth of a second, which is nearer to silicon, where both cores run at
  once.
- It is our own device: no change to QEMU's logic is carried for it.

**Costs, accepted**

- A thousand extra exits from translated code a second, each a few
  microseconds.
- It does not make spinning cheaper: a core that waits on purpose (a
  delay loop, a flash operation's freeze) still takes its half of the host.
  Only running the cores at once removes that, and ADR 0041 is why they are
  not.
- The interleaving of the cores changes, so how often a firmware's own
  cross-core races fire changes with it. ADR 0099's panic is forgiven by
  name where it is (ADR 0124); the slow suite is the check that nothing
  else came with it.
