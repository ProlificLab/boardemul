# ADR 0061 — The station authenticates

**Status:** Accepted — 2026-09-14. Corrects ADR 0060's account of why the
station did not connect.

## Context

ADR 0060 measured that the station never sends an authentication frame, and
put the cause above the MAC: "no frame on the air means nothing for a device
model to answer". That was the right measurement and the wrong conclusion,
and the reason is worth writing down because it is the third time this
subsystem has produced it: **a run's transmit count was read off a log with a
cap on it.** Four lines, then silence, read as four transmits. The device now
prints every frame it is armed with, its subtype and destination, and the
guest's clock beside it -- and with that, the shape of the failure was visible
in one run:

    17s:2  18s:2  19s:1  20s:2  21s:2  22s:3  23s:1

Thirteen probe requests in the first scan, one per channel, and **not one
frame after it** -- not in the nineteen scans that followed, not in the ten
connect attempts. A later run managed twenty-four, another one. The count
varied from run to run with nothing changed. Whatever stopped the station was
a race.

**The transmit queue does seize, and the ROM's part in it is now read.**
Decoding every byte store to offset 18 in the ROM image -- twenty-four sites,
eight in the WiFi code -- shows the ROM writes only 0, 1 and 3 there.
`lmacTxFrame` sets 1 when it arms a slot; `lmacEndFrameExchangeSequence`
clears it only from 5 or 6. Who writes 5 is the flash driver, and single
stepping it from the instruction that reads the completed-slot mask says
exactly when: the transmit-complete handler reads a **per-slot status word at
`0x60034320 - slot*76`**, dispatches on its bits 12..15 (zero is success), and
sets the queue state to 5; the exchange-ender then clears it. Twelve times per
run that cycle completed. The thirteenth time the driver never read the mask.

**The device had told it twice and then taken it back.** A frame's completion
sets bit 7 of the interrupt status register and raises the line. A beacon
delivery *assigned* that register:

    s->regs[R_INT_STATUS / 4] = s->status_mask;

So a beacon landing in the window between a completion and the handler's read
wiped bit 7. The driver never learned that frame had left, its queue stayed
busy, `lmacIsIdle` said no for the rest of the run, and every send after it
-- every later scan's probe requests, the authentication itself -- was
abandoned unsent. Beacons come every hundred milliseconds and a completion is
read within one, so the race is lost about once in a hundred frames: the
first scan's thirteen or twenty-four, and then the one that stuck.

Three earlier instruments said otherwise, and each was wrong in a way that is
now understood: a write watchpoint on the queue byte re-triggers on its own
store (QEMU stops before it lands); a breakpoint taken for `ppTxProtoProc`'s
abandon branch was the `beqz` *testing* `lmacIsIdle`, which fires on every
send whatever the answer; and a breakpoint net that "saw nothing in 200 s" had
all its hits inside the first scan, before the queue stuck, while the poll
that said "busy in 54 of 54 samples" started after it. Both were right.

## Decision

1. **A delivery raises its status bit; it does not assign the register.**
   `|=` where `=` was. One character, and the whole of the above.

2. **The MAC reports busy from arming to completion.** `ic_mac_deinit`
   (0x4003c5fc) spins on bits 13 and 14 of `+0xca0` until both are clear
   before it holds the MAC, and spins on them again after -- read off the
   ROM, literal and all. A real MAC raises them while a frame is on the air,
   so the driver never changes channel under a transmission. The device now
   raises them for the same interval, on read, without storing them, so the
   driver's own hold bits in the same register are untouched.

3. **An answer that finds the receiver off retries at once, not after a
   beacon gap.** The driver switches its receiver off for a moment right
   after it sends an authentication, to reprogram its address filters; the
   answer, built three hundred microseconds after the arm, found bit 8 of
   `+0x0d8` clear and waited a hundred milliseconds behind that gate --
   arriving after the station had given up. On the air the reply is already
   in flight and lands as soon as the receiver is back, so the retry is the
   frame's air time, not the beacon period. Beacons still wait for the next
   beacon.

4. **The log says what every frame was.** Every arm is printed with the
   guest's clock, the frame's subtype and its destination, read from the queue
   entry at the moment of arming; the first frame of each kind after the probe
   request is printed with its body; every answer is printed when it goes on
   the air, not only when it is built. A fixed cap on any of these is what
   produced three wrong conclusions, and a thousand lines is the price of not
   producing a fourth.

5. **One transmit slot, still.** Honouring five, as this family's MAC is
   usually described, was tried in an earlier draft of this work and reported
   as worse -- on a binary that had not been rebuilt, so it was never tested.
   It is not tested now either; one slot carries everything this firmware
   sends, and the count stays at what is measured.

## Consequences

**Good**

- **The station transmits for the whole run**: three hundred and twenty
  frames in two hundred seconds, where the most any run had managed was
  twenty-four, all in its first twenty seconds.

- **The station authenticates.** For the first time on either board, the
  firmware sends an authentication frame -- Open System, transaction one, to
  this access point's address -- and the device reads it, answers it eight
  milliseconds later with transaction two and status zero, and the driver
  takes the answer. ADR 0060's authentication reply, written blind, is
  exercised.

- Every step of the transmit engine is now known from the interrupt to the
  queue state: the arm, the completion, the mask, its acknowledgement, the
  status word the flash driver reads, the outcome it dispatches on, the state
  it sets, and the ROM routine that clears it.

**Costs, accepted**

- **It still does not connect.** After the authentication is answered the
  station does not send an association request; it goes back to scanning
  within twenty milliseconds, and the firmware reports `Connecting Failed`,
  which its own source sets only on reasons 202 and 203 -- an authentication
  or association *failure*, not a timeout. The answer is well-formed by the
  standard, so what the driver objects to is something else, and the
  suspect is the per-slot status word: it lives at `+0x1320`, outside this
  device's page, and reads as zero. Zero is success for a broadcast probe
  request; for a unicast authentication the same handler may well read other
  bits of it -- an acknowledgement, a retry count -- and zero may not mean
  what it means for a probe. Finding out is a step trace of that handler on
  the authentication's completion, which is the next piece of work.

- **The per-slot status register is not modelled**, only known to exist and
  to be read. The device's page is four kilobytes and the register is in the
  next one; modelling it means widening the device, which this ADR does not
  do until what the driver reads from it is known.

- The busy bits are raised for the model's own completion delay, three
  hundred microseconds by default, not for a computed air time. The driver
  only waits for them to drop, so the number does not matter to it; it is
  named here so that the next reader does not take it for a measurement.

- **Reading the frame at arming time is a second read of driver memory** on
  top of the one ADR 0060 made at completion time. Same shortcut, same reason,
  same note in the code beside it.
