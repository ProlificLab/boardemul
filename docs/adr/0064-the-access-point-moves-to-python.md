# ADR 0064 — The access point moves to Python, behind a frame channel

**Status:** Accepted — 2026-09-15. Applies ADR 0006 and ADR 0011 to the WiFi
peer, and adds the one new thing they do not already cover.

## Context

ADR 0063 got the station associated. What stands after it is the WPA2 four-way
handshake, and then a data path: EAPOL-Key messages with an HMAC-SHA1 message
integrity code, a group key wrapped with AES, CCMP on every data frame, and
enough of a peer to answer ARP and DHCP so the firmware gets an address.

**That is not silicon.** ADR 0011 scopes the C tier to SoC silicon and ADR 0006
puts peripheral behaviour in Python, learned from the wire. The access point
this project has been building lives in `esp_wifi_mac.c` because it started as
one beacon and grew a frame at a time -- a body builder, a probe response, an
authentication reply, an association response. Each addition was small. The
handshake is not: by the time it answers DHCP it is a network peer with a
cipher, and none of it is a register.

Counting what the remaining work needs, and where each piece is cheap:

| piece | in Python | in C |
| --- | --- | --- |
| SHA1, HMAC, PBKDF2 | free, `hashlib` | about 190 lines |
| AES-128 | must be written, about 150 | about 150 lines |
| key wrap and CCM | must be written, about 140 | about 140 lines |
| EAPOL, handshake state, ARP, DHCP | about 400 | about 400 lines |

AES has to be written by hand either way -- it is in no standard library this
project is allowed. Everything else is either free on one side or the same on
both, and the C side costs four hundred lines of protocol state in the QEMU
tree on top.

**The one thing the existing architecture does not provide is a way to carry a
frame.** `esp_bus_bridge.c` forwards MMIO and says so in its own header: it
knows nothing about any peripheral, and a register name appearing in it means
the design has gone wrong. Its messages are fixed-size transactions. The WiFi
MAC is not behind it at all -- it is a separate C device because it walks
descriptor chains in guest memory, which a socket round trip per access could
not do.

## Decision

1. **The C device keeps the silicon and nothing else.** Registers, the
   descriptor list and the ordering rule ADR 0063 established, interrupts, the
   forty-eight byte receive header, the check sequence, the transmit slots, and
   the air time a frame takes. It stops knowing what a beacon is.

2. **It gains its own socket, carrying frames.** Not an extension of the bus
   bridge: that file's whole value is that it is about nothing, and a
   variable-length 802.11 frame is about something. A second socket, owned by
   the device that needs it, keeps both honest.

3. **The protocol is synchronous request and reply**, which is the pattern the
   bus bridge already establishes and the guest already tolerates. Every
   request carries the channel the radio is tuned to and may carry a frame the
   guest transmitted; every reply is either nothing or a frame to put on the
   air:

       request   type:u16  channel:u16  length:u16  frame[length]
       reply     length:u16            frame[length]

   Two request types to begin with -- a frame was transmitted, and a tick.

4. **The tick is on the guest's clock, not the host's.** The device already
   has a virtual-clock timer for the beacon schedule, and it keeps it: it asks
   Python what to put on the air rather than letting Python decide when. A
   beacon rate that drifted with host load would change the one thing every
   WiFi test in this suite depends on, and ADR 0063 already found the scan
   sensitive enough to notice which filesystem a test writes to.

5. **The channel travels in the request.** The beacon is built in Python now
   and has to claim the channel it arrived on, so the channel is a field.

   This does **not** retire the shortcut ADR 0057 took, which an earlier draft
   of this page claimed: the device still reads `wDevCtrl+44` out of driver
   memory to fill that field. What changes is that there is now one reader
   rather than a reader per beacon, and that the clean version -- the channel
   coming from the Python radio windows, whose encoding is still undecoded --
   would now need no change on this side at all.

## Consequences

**Good**

- The C device gets smaller, which is the direction ADR 0001 asks for: the
  beacon builder, the body builder and the reply builder leave it, about a
  hundred and seventy lines. The frame check sequence stays -- computing it is
  the radio's job, and the peer would only have to hand it back.

- Python gets SHA1, HMAC and PBKDF2 for nothing, and a test tier that runs in
  milliseconds (ADR 0012) for the cipher it does have to write -- published
  vectors, in the fast suite, rather than a guest boot per check.

- Both reaches into driver memory are still there -- the channel out of
  `wDevCtrl+44` and the transmitted frame out of the buffer the driver pointed
  at -- but they are now the device's only two reasons to look, and each has
  exactly one reader.

**Costs, accepted**

- **A second socket and a second protocol**, which is a real addition to a
  project that has kept to one. It is small and it is versioned by its type
  field, and the alternative was putting a cipher in the QEMU tree.

- **A round trip per tick.** At a beacon every twenty milliseconds that is
  fifty round trips a second on the guest's virtual clock, each one blocking a
  timer callback. The bus bridge blocks a vCPU on every unmodelled MMIO access
  and the guest boots fine, so the budget is there; it is written down so that
  the first person to see a slow run has somewhere to look.

- **A staged move, with a window where behaviour must not change.** The peer
  moves before the handshake is written, so there is a commit whose only job is
  that the scan and the association still work with the frames coming from
  somewhere else. The existing slow tests are what says so, and they earned
  it: the move looked like a regression for a while and was three separate
  things, only one of which was the move.

  The first was this page's own doing. Requiring `--wifi-pass` alongside
  `--wifi-wpa2` -- right, because a handshake cannot be run without it -- made
  the association test exit at start-up, which reads exactly like a station
  that never authenticated.

  The second was an accounting artefact. The device tracked one delivered
  descriptor and asked at the next delivery whether the driver had handed it
  back; once deliveries could follow each other three hundred microseconds
  apart that reported eighty-seven per cent taken on a run whose every scan
  heard a beacon. It keeps the outstanding ones and asks again.

- **The delivery policy had to be measured, and two choices interact.** An
  answer skipping the receive gate is what makes it prompt, and a station
  turns its receiver off for about a hundred milliseconds right after it
  authenticates, so an answer that waits for the gate misses the window and no
  association follows. But an answer that skips the gate also takes a delivery
  the beacon schedule was going to use, and the receiver is off three quarters
  of the time, so beacons starve and no scan hears anything. Neither choice
  works alone; both together do. One run each, nothing else changed:

  | answers prompt | beacon follows | silent scans | associations |
  | --- | --- | --- | --- |
  | yes | no | 33 of 35 | 0 |
  | no | yes | 10 of 14 | 0 |
  | no | no | 24 of 26 | 0 |
  | **yes** | **yes** | **0 of 5** | **2** |

  Both are switchable, because a run-to-run spread this wide is not something
  to take a single measurement of: the same settings gave 1444 beacons in one
  run and 795 in the next.

- **The handshake starts and cannot finish, and the reason is the descriptor
  rule.** The peer builds the first EAPOL-Key message and the device asks for
  it, but between an association response and the disassociation a second
  later, nothing goes on the air at all: the head descriptor holds a frame the
  driver has not picked up, and ADR 0063's rule says the head is the only
  place a frame may go. Every other descriptor in the ring can be free and it
  makes no difference.

  Two things were tried on that and both are recorded rather than kept. The
  peer now declines to spend the last free descriptor on a beacon, which is
  right in itself and was not the constraint -- the counter for it fires while
  the head is the thing that is busy. And the rule was relaxed to "the first
  descriptor the hardware owns, walking from the head", on the argument that
  anything before it must be a frame this device filled, so no gap could open:
  thirty-nine silent scans in forty-three and no authentication at all,
  against none in fourteen. The driver owns descriptors this device never
  filled, so the gap does open and the consumer's count runs past one.

  **Why the driver stops picking frames up is now measured, and it is neither
  of the guesses.** `tools/ring_state.py` waits for the station to transmit an
  authentication and then watches the ring and the cores without stepping
  anything. Through the whole window:

  * the receive interrupt is **asserted and never acknowledged** -- status
    `0x00004000`, bit fourteen, held for seconds;
  * the receiver is enabled (`+0x0d8` = `0x155`) and the MAC is not held;
  * one frame sits in the head descriptor, filled and not handed back, and
    the **other three descriptors are free** the entire time;
  * the head pointer never moves.

  And both cores are at **`PS.INTLEVEL = 3`**, with interrupts pending on core
  zero (`INTERRUPT = 0x100d`, `INTENABLE = 0x1300320d`). Core zero is in a
  compare-and-swap retry loop at 0x40381f9b whose awaited value is
  `0xB33FFFFF`; core one is at 0x403820bc. The literals in that code name it
  without ambiguity: **`spinlock.h`**, **`spinlock_release`**, and the
  assertion **`core_id == lock->owner`**. `0xB33FFFFF` is ESP-IDF's
  `SPINLOCK_FREE`.

  So the driver is not busy and not waiting on us: **both cores are deadlocked
  on an ESP-IDF spinlock with interrupts masked**, which is why the frame is
  never collected and why nothing this side does about descriptors or pacing
  makes any difference. It lasts seconds and ends with the station giving up,
  and it is intermittent -- one run got its first handshake message out, so the
  window is sometimes short enough.

  **And the stall is this project's own accel choice, not the firmware and not
  the cipher.** `s32c1i` is translated with `tcg_gen_atomic_cmpxchg_i32`
  (`target/xtensa/translate.c`), so the instruction is atomic by construction;
  and `emu/run.py` launches `-accel tcg,thread=single`, so the two vCPUs never
  execute concurrently and atomicity could not have been the issue in the
  first place. What settles it is running the other way: with
  `BOARDEMUL_TCG_THREADS=multi` the stall does not happen at all -- the
  interrupt status stays zero, the receive list head **advances**
  (`6b30 -> 6b24 -> 6b18`), all four descriptors stay free, and both cores are
  in ordinary code. The stall is an artefact of single-threaded round-robin
  scheduling: the core holding the lock does not get to run while the other
  spins on it with interrupts masked.

  That did not make `multi` the answer at the time, because ADR 0041 chose the
  single thread for a reason -- `multi` segfaults the emulator, rarely and not
  on an idle machine. So the WPA2 handshake was blocked by the intersection of
  two things this project already knew separately, and the way through was ADR
  0041's defect rather than anything on this page. `--icount` was tried as a
  way of making the single thread's scheduling deterministic instead: it does
  not help, and runs about three times slower in guest time.

  **That blocker is gone.** ADR 0066 found the defect -- a TLB flush performed
  on the wrong thread -- and fixes it, so `multi` is available to a run that
  asks for it. It is still not the default, for the timing reasons ADR 0041
  gives, so the handshake is something this side asks for explicitly.

  What is known to work is the peer: it builds the first handshake message
  correctly and the promise to send it is taken up by the very next tick after
  the association -- measured, `owes=True` on that tick and consumed on the one
  after. One such message reached the air in a hundred and fifty seconds,
  because most attempts stall before getting that far.

- **Writing AES is a correctness risk this project cannot test away entirely.**
  Published vectors say the primitive is right. They do not say the cipher is
  used right, and a CCMP nonce assembled wrongly is a bug the station will
  report as a decryption failure and nothing else. ADR 0065 takes that on.
