# ADR 0062 — The transmit result register

**Status:** Accepted — 2026-09-14. Closes the transmit path opened in ADR 0059
and finishes ADR 0061's open question. Does *not* get the station associated;
that is written down below as a negative result with what it rules out.

## Context

ADR 0061 ended with a suspect: the flash driver's transmit-complete handler
reads a per-slot status word at `0x60034320 - slot*76`, outside this device's
page, and zero might not mean for a unicast authentication what it means for a
broadcast probe request. That suspect is now cleared, and the way it was
cleared is the useful part.

**The word is decoded, completely, from the firmware's own assert strings.**
`lmacProcessTxComplete` reads it through an accessor at 0x420d508c, takes bits
12..15, bounds them at six, and jumps through a table of six addresses at
0x3c18cbc0. The firmware carries the name of every function it can assert in,
so each target names itself:

| bits 12..15 | the driver runs |
| --- | --- |
| 0 | `lmacProcessTxSuccess` |
| 1 | `lmacProcessTxRtsError` |
| 2 | `lmacProcessCtsTimeout` |
| 3 | an assert at `lmac.c:1288` that aborts the firmware |
| 4 | `lmacProcessTxError` |
| 5 | `lmacProcessAckTimeout` |

So **zero is success** -- and zero is exactly what an unmodelled Python trace
window at 0x60034000 had been returning all along. The transmit path has been
getting the friendliest possible answer by accident since ADR 0059. Bits 16..23
are the only other field the success path is handed, as its second argument,
and on the path this firmware takes it is never read. There is no value that
could be put in that register to make the station behave differently, short of
telling it the frame failed.

That also settles what writes the queue state ADR 0060 hunted:
`lmacProcessTxSuccess` writes **5** (at 0x420ce832) and a sibling path writes
**6** (at 0x420cf110, when the state was 1 and the outcome was 4), which are
the two states `lmacEndFrameExchangeSequence` will clear from. Both are in
flash, which is why no ROM store ever wrote them.

**Two real defects turned up while clearing the suspect.** Neither is about the
status word.

The first: an answer was being held behind the receive gate. The driver
switches its receiver off for about a hundred milliseconds immediately after it
sends an authentication, to program its own address into the filter at `+0x000`
and `+0x004`. The model waited for the receiver to come back and then delivered
-- a hundred milliseconds late with beacons at ten a second, and a **full
second** with beacons turned down, because two of the three deferral gates
still waited a whole beacon period even with an answer staged.

The second is why the gate is wrong in principle, not just slow. On the air the
access point's reply leaves ten microseconds after the station's frame, while
the station is still listening for it; it is in the receiver *before* the
driver does anything else. Holding it behind a gate the driver closes
afterwards delivers a frame that physically arrived before the gate shut.

## Decision

1. **The device claims both pages**, 0x60033000 and 0x60034000, and the trace
   window that used to serve the second is gone. A device that does not own the
   last word of its own transmit path is not modelling it. 0x60035000 stays
   unclaimed, for the reason ADR 0008 gives: the hardware RNG lives there.

2. **The result is written per slot at completion**, as success, deliberately.
   It is the same zero the window returned; the difference is that it is now a
   stated fact with the table above beside it, and a future outcome -- a real
   acknowledgement timeout, a collision -- has a place to be written.

3. **A staged answer waits its own air time, and nothing else.** All three
   deferral gates use one helper, and an answer is exempt from the
   receiver-off gate entirely, because it arrived before that gate closed. A
   free descriptor is still required: that is the one resource a real MAC
   genuinely cannot do without.

4. **The network it advertises is an ordinary one.** Four 802.11b rates and no
   ERP element described a pure-b access point carrying an RSN element, which
   is not a network that exists anywhere, so no station's rate and phy-mode
   negotiation has ever had to cope with one. It now offers the eight rates a
   g network offers, an ERP element, and the rest in an extended element.

5. **There is a hatch for the trace that would settle the rest.**
   `BOARDEMUL_WIFI_MARK=0xb0` makes the device raise a cell at `+0x1ff8` --
   somewhere in its span the driver never touches -- when the station sends
   that subtype, and hold *that* answer back until a debugger writes the cell
   to zero. It exists because the obvious instrument does not work: a
   breakpoint on the receive path fires for every frame, which slows the guest
   about fiftyfold, and a guest that slow never reaches an authentication at
   all -- one attempt got eight seconds into the firmware for ten minutes of
   waiting. With the hatch there are no breakpoints until the frame that
   matters exists, and then exactly one stop, on the right frame, by
   construction. It is inert unless the variable is set, and it is a debugger
   channel rather than a register, which is said here and in the code because
   it is the one thing in this file that is not modelling hardware. The
   debugger side is `tools/trace_rx_frame.py`, kept rather than left in
   `logs/` because it is the instrument the next two steps need.

## Consequences

**Good**

- **The transmit path is owned end to end.** Arm, completion, completed-slot
  mask, its acknowledgement, the result word, the outcome the driver
  dispatches on, the queue state it sets, and the ROM routine that clears it:
  every step is either modelled here or read out of the image, and none of it
  now depends on an unclaimed window's incidental zero.

- **The authentication reply lands one millisecond after the frame it answers**,
  where it was a hundred milliseconds to a second before. That is what the
  standard's timing looks like, and it is measured: arm at 32788 ms, reply on
  the air at 32789 ms.

- Every probe request in a two-minute run is answered -- 197 of 197 -- and the
  firmware finds the network on fourteen of nineteen scans with beacons turned
  down to one a minute, which says the whole answer path works for unicast
  frames and not only for broadcasts.

**Costs, accepted**

- **The station still does not associate.** The shape of the failure has not
  changed through any of it: one authentication frame per connect attempt,
  answered in a millisecond, then thirteen seconds of silence and
  `Connecting Failed`. No association request is ever built. Ruled out, each
  by measurement, before the cause below was found:

  - the transmit result word (zero is success, and success is what it read);
  - the reply's latency (a hundred milliseconds, then one);
  - unicast delivery, addressing and the receive filter (a scan finds the
    network on probe responses alone, with no beacons);
  - the receive header's rate byte (64, 65, 120 and 127 all behave alike);
  - the advertised rate set, ERP element and phy mode.

  The reply itself is well formed: open system, transaction two, status zero,
  from the address the station addressed its request to, thirty bytes and a
  check sequence.

- **Where the reply dies is now known exactly, and it is in the ROM.** The
  step trace ran: the authentication reply reaches `wDev_ProcessRxSucData`,
  is walked for three hundred and twenty instructions, reaches
  `wDev_IndicateFrame` -- and is handed straight to **`wDev_DiscardFrame`**.
  The 802.11 stack never sees it. So every content-level suspect above was
  ruled out for the right reason: the frame is thrown away before anything
  could object to what is in it.

  The gate is two comparisons at the top of `wDev_IndicateFrame` (0x40040f30).
  It indicates the frame when its first argument's low byte is **zero**, or
  when its fourth argument is **one**; anything else goes to the discard
  function pointer at `wDevCtrl+460`. This reply arrives with a 1 and a 4.

  **Corrected, 2026-09-14, same day.** The first draft of this bullet said the
  path was chosen by byte 47 of the receive header and that 245 was worth
  trying there. That was wrong twice over, and the second error is the
  interesting one.

  Byte 47 cannot matter: the two instructions that read it are

      beqz a12, 0x40041231          ; 0 goes there
      movi a3, -245 ; add a3, a12, a3
      beqz a3, 0x40041231           ; and so does 245

  -- both accepted values jump to the *same* address, and every other value
  goes down a channel-state-information path that ends in the same discard.
  On the path this model takes, byte 47 is read once and decides nothing.

  **The claim came from a defect in `tools/disas_rom.py`.** A BI0 branch's
  four-bit field is an *index* into `B4CONST`, not the constant, and the
  decoder was printing the raw index. Indices one to eight are their own
  value, so it read correctly for every constant this project had looked at
  until now -- and then printed a branch comparing against "13", which
  `B4CONST` cannot express. The two branches were really `bnei a9, 64` and
  `bnei a9, 128`: a probe request and a beacon. Fixed, with the reason beside
  it, because a disassembler that is right about small numbers and quietly
  wrong about large ones is worse than one that is wrong about both.

  **What actually decides it is the second argument**, and that is now
  measured on both sides. `wDev_IndicateFrame` passes a frame to the stack
  when that argument is **1** and discards it otherwise, and the same
  instrument caught both cases on the same firmware:

  | frame | descriptor | second argument | outcome |
  | --- | --- | --- | --- |
  | beacon | 0x3fcb6b0c | **1** | passed to the stack |
  | authentication reply | 0x3fcb6b18 | **4** | `wDev_DiscardFrame` |

  So beacons are not saved by being beacons -- they take the same management
  path and reach the same flag -- they are saved by that gate. Which also
  explains a number that never quite fitted: with beacons ten a second enough
  of them draw a 1 for a scan to find the network, fourteen scans in nineteen
  and never all of them, while an authentication reply comes once per connect
  attempt and gets one throw of the same dice.

  **A model change aimed at that was tried and reverted.** The device kept
  whichever descriptor the driver had freed most recently; making it prefer
  the lowest-addressed one instead -- on the theory that the beacon's 1 came
  from landing in 0x3fcb6b0c -- made it strictly worse: twelve scans out of
  twelve found nothing, and the station never got as far as authenticating.
  So the argument is **not** a function of which descriptor the frame lands
  in, and the two-point correlation above was a coincidence. Reverted, and
  the scan verified back at eleven scans with nothing missed.

  **The next step is therefore in the driver, not the header.** The argument
  is assembled by the driver's own IRAM caller: at 0x4037bdd6 it reads a byte
  from a table, indexed by a four-bit value it stashed at 0x4037aa84+170 from
  the result of a dispatched call, and passes it as the second argument
  through `pp_wdev_funcs[125]` at 0x4037bdd9. A step trace of *that* caller,
  on a frame that draws a 1 and a frame that draws a 4, says what the device
  would have to do differently. `tools/trace_rx_frame.py` reaches it by
  breaking at 0x4037bda0 instead of the ROM entry.

- **Two ADR 0061 statements are corrected.** The status word is not "outside
  this device's page" any more, and it was never a plausible cause; and
  `ieee80211_auth`, `ieee80211_assoc` and `ieee80211_sta_retry_assoc`, found by
  name in the image, are feature-gated shims that run **not once** in a
  hundred and seventy seconds. Finding a function by its name proves nothing
  about whether it is on the path.
