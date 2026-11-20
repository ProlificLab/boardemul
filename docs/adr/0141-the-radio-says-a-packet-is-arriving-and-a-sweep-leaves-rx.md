# ADR 0141 — The radio says a packet is arriving, and a sweep leaves RX

**Status:** Accepted — 2026-11-18. Re-derives a fix drafted on 2026-10-03 and
lost before it was committed. Closes ADR 0111's exception for a CAD on the
wall-clock air. Amends ADR 0113's judge.

## Context

`test_mesh` lost packets in a loaded suite and passed alone. On 2026-10-03 a
session traced it to the radio model, not to the test, and drafted a fix in a
worktree. The worktree was removed before the fix was committed, and nothing
of it survives but its notes. This re-derives each defect from the sources in
`logs/ref` (the SX1261/2 datasheet, Rev. 1.2; RadioLib; Meshtastic's
`SX126xInterface.cpp`) and the model as it is.

Under load the wall-clock air stretches every step of Meshtastic's
listen-before-talk -- GetIrqStatus, standby, CAD, write the buffer, SetTx --
from milliseconds to hundreds, because each is a socket round trip to Python
at the host's speed. Four defects that a quiet host never reaches then bite:

1. **A packet arriving is never announced.** The model raises RxDone at a
   packet's end and nothing before it. The chip raises PreambleDetected
   (bit 2) when it detects a preamble and HeaderValid (bit 4) when it has
   decoded a valid header (Table 8-4). Meshtastic's `isActivelyReceiving()`
   reads exactly those two, and does not transmit while either is set. With
   neither, a node keys its transmitter on top of a packet it is receiving:
   the model counts the reception cut off, and the packet is lost.

2. **Enabling a flag and routing it to DIO1 are one thing to the model.**
   SetDioIrqParams carries IrqMask, then Dio1Mask (13.3.1). A flag is logged
   only if IrqMask enables it, and raises DIO1 only if Dio1Mask also routes it
   (13.3.2.1). The model latched every flag, enabled or not, and drove DIO1 by
   IrqMask. Meshtastic's receive enables RxDone, PreambleDetected,
   HeaderValid, HeaderErr, CrcErr and Timeout and routes RxDone alone, so
   raising PreambleDetected through the model as it was would have pulsed
   DIO1 at every preamble, which the chip does not.

3. **A sweep on the wall-clock air leaves the chip in RX.** After a CAD that
   finds nothing the chip returns to STBY_RC (13.4.7). ADR 0111 left the
   model in RX on the wall-clock air, because its CAD detected nothing there
   and the mode change alone lost packets. In RX after the sweep, a packet
   that arrives while the host writes its own packet into the buffer and
   before SetTx is received into that same buffer, and the node then
   transmits the other node's packet. The 2026-10-03 notes record seeing
   exactly that on the air: a first-hop frame carrying the other node's
   `from` and relay byte.

4. **On the shared-clock air the judge credits a packet to a radio that had
   left RX.** `_judge` locks a packet if the radio was in RX at its start, or
   entered RX in time, and schedules RxDone at its end. It does not look at
   whether the radio left RX after locking and before the packet became
   known at a barrier; `_set_mode` only cuts off a reception that was already
   known when the mode changed. The packet is then received by a radio that
   was transmitting. Re-deriving this showed a second gap in the same place:
   the judge counted a radio as locked if it was in RX for an instant within
   the preamble, though locking takes the symbols it already names
   (`lock_symbols`). A radio in RX for nine microseconds and then
   transmitting was counted as cut off from a packet it never locked on to,
   where it was deaf.

And the test itself:

5. **`test_mesh` counts who spoke by the packet's `from`.** A relayed packet,
   or one sent by mistake as in 3, carries another node's `from`, so a node
   that never spoke can be counted. The air knows which board each frame came
   from: the port it was sent from, which each board's file in the air
   directory names.

## Decision

1. **SetDioIrqParams keeps IrqMask and Dio1Mask apart.** A flag is logged
   only if IrqMask enables it; DIO1 is high while a logged flag is routed by
   Dio1Mask.

2. **A reception raises PreambleDetected, then HeaderValid, then RxDone.**
   - PreambleDetected when the receiver locks: the later of the packet's
     start and the radio's entry into RX, plus the symbols a receiver needs
     to lock (`lock_symbols`, ADR 0113). The datasheet gives no time.
   - HeaderValid at the end of the header: the preamble, its 4.25 symbols of
     sync, and the eight symbols the time-on-air equation always counts first,
     which carry the header at coding rate 4/8 (6.1.3.1, 6.1.4). Explicit
     header only; implicit has none.
   - Leaving RX before either cancels it, as it cancels RxDone.

3. **A sweep behaves the same on both airs.** On the wall-clock air, the
   radio keeps the packets it heard for a CAD to look back on, as the shared
   air does, and a sweep that overlapped one by a symbol detects it. The chip
   goes to STBY_RC after the sweep, or stays in RX on detection when told
   CAD_RX. ADR 0111's exception is closed.

4. **A radio locks on only if it stays in RX for the lock's symbols**, entering
   by the preamble's deadline as before. Otherwise it was deaf if it
   transmitted meanwhile, or not listening. A packet it did lock on to and
   then left RX before its end is cut off, not received: the judge reads the
   radio's history from the lock to the packet's end, as far as it is known,
   and a reception still in flight is cut off when the radio leaves RX, as
   before.

5. **`test_mesh` counts a sender by the board its frames came from**, and
   its ceiling rises from 300 s to 600 s: under the heaviest load measured on
   2026-10-03 (sixteen guests on fourteen cores) a node sent nothing in 300 s.

## Consequences

**Good**

- A node no longer transmits over a packet it is receiving, sends another
  node's packet as its own, or is credited with a packet it could not have
  received.
- Each rule is the datasheet's or the firmware's own reading of the chip.

**Costs, accepted**

- PreambleDetected's time is the lock figure ADR 0113 already uses, not a
  measurement.
- One defect from 2026-10-03 stays open: after a firmware assert and reboot
  the radio was found at its power-on settings (frequency 0, SF7) with no
  reconfiguration, and a node counted wrong-frequency packets. Its cause was
  not found; the radio logs its resets and cold wakes at INFO so the next one
  can be.
  - *Seen again 2026-11-20*, in a Meshtastic touch test without `-icount`,
    and the assert named: `SX126xInterface.cpp:292`, `assert(err ==
    RADIOLIB_ERR_NONE)` in `startReceive()`, after
    `SX126X startReceiveDutyCycleAuto RadioLib err=-705` at guest 10 s,
    two seconds after `SX126x init result 0`. -705 is RadioLib's SPI command
    timeout: BUSY did not fall within its wait, before or after a command.
    The controller's own count of that BUSY line showed only short holds (the
    longest 8.4 ms), but it counts holds that ended, so one that never fell
    would not show. The model holds BUSY until the next select only after
    SetSleep or while NRESET is low, and the run's log shows no reset until
    the reboot the assert caused. Twenty boots with every bus transaction
    logged (`logs/dbg/radio_hunt.sh`) did not repeat it. The touch test, which
    allows only ADR 0099's panic, fails on it when it comes, as it should.
  - *A lead, the same day (ADR 0174):* that run's output folder was on the
    host-shared mount, where QEMU wrote its log line by line from the
    guest's own thread over 9P; the twenty clean boots wrote to local disk.
    Without `-icount` the guest's clock is the host's, so one long stall of
    that thread inside RadioLib's wait for BUSY reads, to the guest, as BUSY
    held past the library's timeout. Since ADR 0174 those writes no longer
    happen on the guest's thread. Not proven; the next occurrence, or none,
    will say.
