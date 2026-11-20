# ADR 0176 — QEMU is profiled by sampling, and trace windows are read from a shadow

**Status:** Accepted — 2026-11-20. Gives ADR 0161's speed work the profile
it lacked; amends ADR 0011's bridge protocol.

## Context

Emulation speed has been the standing limit (ADRs 0161, 0174). What was
known of it came from the guest's side -- QEMU's TB statistics, its
exception log, the hot-blocks plugin -- and the host's side, where QEMU
itself spends its time, needed a profiler. perf and gdb are not in the
build container, and installing them is a download to be agreed.

ptrace works on a process's own children in the container, without the
`CAP_SYS_PTRACE` Docker drops: strace runs there. A sampler needs nothing
more: start QEMU as a child, seize each of its threads, and every
millisecond stop each one for an instant and record its instruction pointer
and the words above its stack pointer that point into code.

Its first profile, a 40 s Meshtastic boot with WiFi set (a device folder on
local disk, without `-icount`), 2026-11-20, of the thread that runs the
guest:

| | share |
|---|---|
| generated code | 25 % |
| TB lookup and the exec loop (`cpu_exec_loop`, `qht_lookup_custom`, `cpu_get_tb_cpu_state`, ...) | 14 % |
| waiting for the big QEMU lock, against the main loop | 9 % |
| `recv` in the register bridge (`bridge_mmio_read`), and SPI/I2C | 10 %, 1 % |
| register-window helpers, MMU | 5 %, 3 % |

The bridge's share is reads of windows nothing models: `fe` and
`radio_unknown`, the radio front end and its state page, which the WiFi
PHY's start-up writes and reads back some 40,000 times (ADRs 0019, 0054). A
read waits for Python to answer, behind the writes posted before it: about
165 µs each. Behind both windows is a `TraceRegion` -- a read returns what
was last written at that offset -- and, for the gated addresses, `PhyGates`,
which forces a register's completion bits high on read. Nothing Python does
there needs Python, but the windows are claimed so that every access is
counted: what a run's record says the firmware touched (ADR 0053).

## Decision

1. **`tools/qemu_sampler.c` and `tools/sample_report.py`**, with
   `tools/profile.sh` to build the sampler in the build container, run a
   `run.py` command under it and report: per thread, the share of samples in
   each function of QEMU or its libraries, or in generated code; and with
   `--callers NAME`, the QEMU functions found above NAME on the stack.
   Scanned words, not an unwinder, so callers are a guess, named as one.

2. **A trace window is read from a shadow.** Its bridge device keeps, in
   QEMU, what the Python model would answer: per offset, the last value
   written, ORed with the gate bits for that address, cleared at reset. A
   read is answered from it at once, and posted to Python as a notice that
   carries the value (`BRIDGE_OP_READ_SEEN`), so the access is counted and
   kept in the recent tail as before, and nothing waits. Writes and resets go
   to Python as they did. Only windows whose read is that rule exactly are
   shadowed (`TraceRegion`, `PhyGates`); every model with behaviour stays
   answered by Python.

## Measured

The same boot profiled with the shadows, the same day: `recv` on the
guest's thread 2.6 % (from 10.0 %), `send` 0.9 % (from 3.2 %); the bridge's
count of accesses to `fe` and `radio_unknown` unchanged in kind. Under
`-icount`, where the guest does the same work whatever the host, a WiFi
device reached 19.5, 18.6 and 19.5 guest seconds in 60 s of wall time with
the shadows and 18.2, 18.9 and 18.2 without (`BOARDEMUL_SHADOW=0`,
interleaved): 4 % more, over runs of which only the last seconds are the
PHY's start-up. `tests/test_bridge_shadow.py` drives writes and reads of
every kind through QEMU's qtest and checks each read against the Python
model; the Bluetooth link-layer, connection and pairing tests, the WiFi
join and the T-Deck and Max boots pass with them.

The profile's next item is the big lock: the main loop wakes about 2,500
times a second on a Meshtastic boot, and each wake is a handoff the guest's
thread waits through (9 %, and RCU's 3 %). A thousand of those wakes are
ADR 0161's turn timer. Tried the same day and not kept: ending the turn
from a thread of its own, which `cpu_exit()` allows without the lock. The
main loop then blocked 1,800 times a second instead of 2,500 and the
guest's thread ran 73 % of the time instead of 67 %, but Meshtastic reached
its home screen no sooner (`tools/speed.sh`, three interleaved runs each:
11.2 s both ways, spread 3 s). Most of the rest are the firmware's own
timer interrupts, two cores at 1 kHz, which without `-icount` the main loop
has to raise.

## Consequences

**Good**

- Most of the register bridge's 10 % of the guest's thread on a WiFi boot
  goes, with the run's record of what the firmware touched unchanged.
- Speed work can measure the host's side from now on, inside the container,
  without installing anything.

**Costs, accepted**

- The read rule is written twice, in `emu/board.py` and in the bridge
  device. A test drives the same accesses through both.
- A shadowed window's value no longer passes through Python, so a model
  that later gives such a window behaviour must stop shadowing it; the
  class says whether it is shadowed, so changing the class does it.
- The sampler stops each thread a thousand times a second; it slows what it
  measures by a few percent, which the shares above include.
