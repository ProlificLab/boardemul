# ADR 0060 — An access point that answers

**Status:** Accepted — 2026-09-13. Builds on ADR 0059.

## Context

ADR 0059 got transmissions to complete. What it left was an access point that
beacons and says nothing else: the station could speak, and nothing replied.

**The frame the station sends is readable, and it is not what ADR 0058
assumed.** `lmacTxFrame` stores the buffer it was given into word 0 of the
queue entry (0x4003b2e6), and that buffer holds a pointer to the frame at +16
with its header and body lengths as halfwords at +20 and +22. Dumped at that
instruction:

    buffer +16  88 95 cb 3f  18 00 23 00
    frame  +0   40 00 00 00 ff ff ff ff ff ff 02 54 44 4d 00 01
    frame +16   ff ff ff ff ff ff 00 00 00 0d 78 69 6e 79 75 61
    frame +32   6e 64 69 61 6e 7a 69 01 08 8b 96 82 84 0c 18 30

A first octet of 0x40 is subtype 4: a **probe request**, from
`02:54:44:4d:00:01` -- the station's own address, matching its efuse -- naming
`xinyuandianzi`, twenty-four bytes of header and thirty-five of body. So the
scan is *active*, and ADR 0057's "this firmware's scan is passive" was wrong:
it was measured on a run where the queue had already seized, so the probes had
stopped.

## Decision

1. **Read the frame, and answer the three that want answering.** The subtype
   comes out of the request's first octet and the reply is addressed to
   whoever sent it:

   | request | answer |
   | --- | --- |
   | probe request (0x40) | probe response (0x50), saying what a beacon says |
   | authentication (0xb0) | open system, transaction two, success |
   | association request (0x00) | accepted, with association identifier one |

   A frame that is not management, or not addressed to this access point or
   the broadcast address, is not answered.

2. **A reply goes out the way a beacon does.** The same descriptor walk, the
   same forty-eight byte header, the same conditions -- a free descriptor and
   a MAC that is listening. One reply waits at a time, because a station sends
   one frame and waits for its answer before sending the next.

3. **It is delayed by the frame's air time, not sent in the same instant.** On
   the air the frame leaves and the answer comes back after it, so the reply is
   armed on the same timer the transmit completion uses.

4. **The beacon builder is split** so a probe response can share its body,
   which is the same fixed fields and elements. One description of the network,
   used twice.

## Consequences

**Good**

- **The emulator reads what the firmware transmits.** That is new: until now
  nothing on either side of this project had ever looked at a frame the guest
  sent. It is logged with its subtype and its sender, so a run says what the
  station said.

- **Probe requests are answered, and the answer is delivered.** Four probe
  requests, four probe responses of eighty-two bytes, each staged as a
  hundred-and-thirty-four byte buffer -- forty-eight of header, the frame, and
  its check sequence -- and taken by the driver through the receive path that
  already worked.

- The authentication and association answers are built by the same code and
  the same rules, so when the station can send those frames they are waiting.

**Costs, accepted**

- **The station still does not connect, and the reason is now exact.** Its
  LMAC transmit queue reads busy in **fifty-four samples out of fifty-four**
  across two minutes of connect attempts, so `ppTxProtoProc` abandons every
  send and the station never gets as far as authentication. The four probe
  requests all happen in the first twelve seconds, before the queue seizes.

  So the authentication and association answers in this ADR are **written but
  not exercised**. They are the answer to a question the station cannot yet
  ask.

- **What seizes the queue is now known to be outside this page.** Everything
  the driver reads after a transmission is answered -- the completed-slot mask,
  its acknowledgement, the status and the position -- and it reads nothing
  else. The queue's own state byte is set to 1 by `lmacTxFrame` and cleared
  only by `lmacEndFrameExchangeSequence`, whose first path requires that byte
  to be **6** before it will clear it. Nothing in ROM writes 6 there. So the
  driver's transmit state machine has a step between "handed to the MAC" and
  "finished" that this model does not satisfy, and finding it is the next
  piece of work: it is a state machine in the flash driver, not a register.

- **Reading the frame means reading driver memory**, through the ROM's LMAC
  queue table at 0x3fcef308. That is the same shortcut as reading `wDevCtrl+44`
  for the channel and is called out in the code for the same reason: a real MAC
  fetches the frame by DMA from where the driver pointed it, and this is where
  the driver pointed it, but no wire carries it.

- **And a connection needs more than association.** The WPA2 four-way
  handshake still stands after it, and its AES-CCMP is not in the standard
  library (ADR 0059).
