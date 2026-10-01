# ADR 0113 — Boards on one air keep one time

**Status:** Accepted — 2026-10-01. Finishes what ADR 0112 started. Closes the
last of ADR 0111's accepted costs (where the chip goes after a CAD).

## Context

ADR 0112 put everything a radio does by itself on its own guest's clock. A
packet from another board is a different matter. Each board is its own QEMU,
with its own virtual clock, running at its own speed. A packet one board
started at its 10.000 s reaches another whenever the host delivers it, and by
then that board may be anywhere. So ADR 0112 judged receptions on the
receiver's own clock, read from a 10 ms tick. Three things stay wrong while
that is so:

- **Listen-before-talk does nothing.** A CAD cannot see what is on the air at
  a guest instant, so it never detects anything. Meshtastic sweeps before every
  transmission (`isChannelActive()`), and two nodes that boot together still
  talk over each other (ADR 0098).
- **The chip's mode after a CAD cannot be corrected.** ADR 0111 found that
  moving it to STBY_RC without detection loses packets.
- **Collisions and deafness are judged at whatever moment the host happens
  to deliver a frame.**

What it takes is a single time that every board keeps. Under `-icount`
(ADR 0027) QEMU can provide one, and this is the fact the design rests on, read
from `accel/tcg/tcg-accel-ops-icount.c`:

- The vCPU's instruction budget never passes the next virtual-clock deadline
  (`icount_get_limit`).
- When a timer falls due, the vCPU thread runs it itself
  (`icount_notify_aio_contexts` calls
  `qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL)`).

So a timer callback that blocks stops the guest's time at an exact
instruction. Without `-icount`, virtual timers run on the main loop while the
vCPU keeps executing, and virtual time follows the host's clock. Blocking there
stops nothing.

## Decision

1. **Boards on one air advance together, in quanta of guest time, under
   `-icount`.**
   - `--air-boards N` says how many boards keep time on the air, and it
     requires `--icount`.
   - A board waits at time zero until all N are present.
   - At every multiple of the quantum, a board waits until every other timed
     board has reached it.
   - The quantum is 1 ms unless `--air-quantum-us` says otherwise.
   - The guest's clock does not sleep (`-icount sleep=off`). When the guest
     idles, its time jumps to the next deadline instead of following the
     host's clock. QEMU documents this as deterministic, and with
     `sleep=on` an idle guest's time would advance by however long the host
     happened to take.

2. **The barrier is the guest clock's tick made synchronous** (ADR 0104).
   - In this mode the device sends its time each quantum and waits for an
     answer before the guest runs on.
   - The answer lists lines to move, each as pin, level now (or leave it)
     and next rise. They are applied with `esp_gpio_drive` and
     `esp_gpio_schedule` (ADR 0112).
   - This is how a reception reaches DIO1 at its guest time. On a timed air,
     the radio never drives its line from Python.

3. **Boards exchange the barrier over the air itself.**
   - At each barrier a board sends a time mark to every timed peer. It sends
     it from the socket its frames go out on, after any frame it sent in that
     quantum.
   - A board goes on once it holds a mark at least that late from every
     timed peer.
   - Loopback delivers one socket's datagrams in order, so every frame a peer
     sent before its barrier is in hand by the time its mark is.
   - A board's `.air` file says whether it keeps time. A passive listener,
     like the mesh test's, does not, and is not waited for.
   - A peer whose file has gone has left, and is not waited for either.

4. **A packet is judged at the barrier after it starts, on the shared clock**,
   in the order of the senders' start times:
   - It starts at its sender's start time (ADR 0112: the release plus the
     ramp) and ends its time on air later.
   - A receiver transmitting at its start is deaf to it.
   - A receiver hears it if it is in RX at the start, or enters RX while at
     least the minimum symbols of preamble are still to come.
     - The datasheet gives no such minimum; it says only that preamble
       detection "periodically restarts" (6.1.3).
     - The figure used is the one the firmware's own library assumes when it
       sizes a duty cycle: RadioLib's `calculateRxDutyCycle`, 8 symbols, or 12
       at SF5 and SF6.
   - The receiver's modes are kept as a short history, with the exact times
     its segments gave (ADR 0112).
   - Two packets that overlap at a receiver collide.
   - RxDone is due at the end, and DIO1 rises then if it is routed, through
     the barrier's answer.

5. **A CAD detects what is on the air, and then returns to STBY_RC.**
   - It detects a packet on its frequency, strong enough to decode, that
     overlaps the sweep's symbols by at least one symbol. 6.1.5 says CAD
     reports "if LoRa symbols have been detected during the CAD".
   - CadDetected is raised with CadDone.
   - The chip then goes to STBY_RC, or stays in RX if the sweep detected
     something and the exit mode was CAD_RX (13.1.8, 13.4.7).

6. **What a quantum costs in exactness is said, not hidden.** A packet becomes
   known to other boards at the latest one quantum after its sender's
   release. It starts a ramp after that release:
   - **Out of STBY_RC with a TCXO:** the ramp is over 5 ms, so the packet is
     known before it starts.
   - **Out of FS or STBY_XOSC:** the ramp is 76–105 µs, so the packet can
     be up to a quantum late.

   A CAD therefore sees every packet that started at least one quantum before
   it ends. Receptions are exact, since their outcome is decided at their end,
   tens of milliseconds later.

7. **The test is the shared clock's physics. Determinism was the aim, and
   it is not reached yet.**
   - `tests/test_mesh_timed.py` runs two Meshtastic nodes on a timed air. It
     asks that each transmits and that a packet crosses. It also asks that
     every packet heard ended, on the receiver's clock, exactly when the
     sender's packet ended: its start plus its time on air, to the
     microsecond. That holds whatever the host is doing, because both ends
     keep one time.
   - The aim was more: the same boards, run twice, putting the same packets
     on the air at the same guest times. Measured with two copies of one pair
     on two airs at once, both carried the same exchange (one sends, then
     two, then one hears two). The times differed, though: by 0.25 ms in one
     run and by 55 ms in another, at the first transmission, about 36 s in.
   - The search for the cause closed three real leaks, decisions 1, 8 and 9,
     and ruled out these:
     - the barrier itself: plain `-icount sleep=off` runs, with no barrier,
       diverge too;
     - the host's GPIO messages: all land at guest time 0;
     - the Python models' answers: every bridged read returns the same value
       in every run;
     - the radio's BUSY: it falls at the same instant and is seen on the same
       poll;
     - virtual-clock timers: under `-icount` the main loop does not run them
       (`qemu_clock_use_for_deadline`);
     - cutting a vCPU's budget at realtime deadlines: an experiment that
       stopped it changed nothing.
   - What remains appears only under host load, a few seconds into a boot, as
     a few hundred guest instructions more in one run than another. It is
     somewhere in QEMU's execution of the two cores.
   - Until it is found, a timed air is exact at every instant it keeps, and
     the same within a run, but not yet identical between runs.

8. **Interrupts a device defers past the guest's access are deferred on the
   guest's clock.**
   - The I2C master, the Bluetooth core and the USB serial port each raise
     the interrupt an access causes after the access, never inside it. Inside
     it, the interrupt lands on the very next instruction and re-enters the
     driver that started the transfer (`esp_i2c_master.c`).
   - They did that with a bottom half, which runs whenever the main loop next
     takes the lock. That is a host-dependent number of guest instructions
     later.
   - The acceptance test found it. Two runs of one timed board differed by a
     millisecond within six seconds of boot, and nothing they were fed
     differed. Meshtastic talks over I2C and USB all the time.
   - The deferral is now a timer, 10 µs of guest time after the access
     (`ESP_IRQ_DEFER_NS`). It is still after the access, as before, but at an
     exact instruction.

9. **The host's levels on the board's input pins are in place at time
   zero.** The first barrier's answer carries every level the host has set:
   the trackball's idle lines and the touch controller's interrupt at rest.
   Over the GPIO socket alone they land whenever QEMU's main loop next takes
   the lock, which under load can be after the guest's first instructions.
   The touch interrupt's resting level is now set when the board is built,
   not on the ticker's first pass.

10. **A run says how its radio fared on the shared air.** The manifest's radio
   record gains three counters:
   - `cut_off`: receptions lost because the radio left RX part-way through;
   - `cad_sweeps`: CADs run;
   - `cad_detected`: CADs that detected something.

   Every thousandth barrier logs the guest time and the wall time each
   barrier took, and how much of that was spent waiting for the others.

## Consequences

**Good**

- Listen-before-talk works, so two Meshtastic nodes back off from each other
  as they would on silicon.
- A mesh run's outcome follows from the firmware and the scenario. It no
  longer depends on how the host scheduled two processes.
- Physics is right at any host speed: airtimes, collisions, deafness, CAD.

**Measured.** Two Meshtastic nodes, 900 s of wall time, quantum 1 ms
(`logs/timed_mesh.py`), all times guest:

- Node one sent at 36.691 s, 1010 ms on air.
- Node two swept the channel ten times while that packet was on the air,
  detected it eight times, and transmitted at 37.755 s, after it had ended.
- One heard that packet at 38.765 s, its last bit.
- Two sent again at 66.883 s, and one heard it at 67.483 s.

Each guest millisecond took 3.5–4.5 ms of wall time, about half of it waiting
for the other board. Later, with node two's interface busy, it took 10–15 ms.

**Costs, accepted**

- Runs are not yet identical between runs (decision 7). Every timing a run
  has is exact on the shared clock, but two runs of the same boards can
  disagree by milliseconds after the first few seconds.

- A timed air needs `-icount`, which is slower. Meshtastic ran at about a
  fifth of wall time under it, and every board on the air runs at the pace of
  the slowest.
- One exchange per quantum per board: a thousand UDP round trips on loopback
  per guest second.
- A board that hangs, rather than leaving, stops the others with it. That is
  what a shared clock means. A run's timeout still ends it.
- The minimum preamble for a late lock-on is the firmware library's
  assumption, not a measurement of the silicon.
- **No PreambleDetected or HeaderValid.** The radio never raises either
  interrupt, so a receiver mid-packet does not look busy to its firmware.
  Meshtastic checks exactly those flags (`isActivelyReceiving()`) before its
  listen-before-talk. In the measured run, node two, with a packet to send,
  swept the channel while it was receiving one's first packet. Sweeping took
  it out of RX, so it lost a packet that silicon would have received.
  The datasheet says when neither flag rises: only that a header coded at 4/8
  follows the preamble (6.1.3). They wait for a published source.
