# ADR 0060 — An access point that answers

**Status:** Accepted — 2026-09-13. Builds on ADR 0059. Its account of why the station does not connect is corrected by ADR 0061.

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

- **The station still does not connect, and what it never does is
  authenticate.** Over two hundred seconds, twenty scans and ten connect
  attempts, every frame it transmits is a probe request: the device logs the
  first frame of each subtype it sees, and subtype four is the only one that
  ever appears. No authentication frame, no association request. So the
  authentication and association answers in this ADR are **written but not
  exercised** -- they answer a question the station never asks.

  The firmware's own verdict agrees and is more specific than before: the
  first attempt fails with `Connecting Failed AP not found`, and the nine
  after it time out with no reason at all. So the network is found, the
  connect begins, and it ends without a frame on the air.

  (ADR 0061: this was measured on runs whose transmit queue had already
  stuck, on a race in this device -- a beacon delivery wiping a pending
  transmit completion. With that fixed the station transmits for the whole
  run and does authenticate. "Every frame it transmits is a probe request"
  was true of those runs and is not true of the station.)

- **Three earlier claims about why were wrong, and they were wrong because of
  the instruments.** They are written out because each one cost a run:

  - "`ppTxProtoProc` abandons every send." Never measured. The address taken
    for the abandon branch, 0x4003d809, is the instruction *after* the call to
    `lmacIsIdle` -- `beqz a10, ...` -- so it is reached on every test whatever
    the answer. It fires six times while the queue reads idle.

  - "The station transmits four times in two hundred seconds." That was a
    logging cap of four, not a count. `lmacTxFrame` runs about every two
    hundred milliseconds for the whole run. The log now reports the first
    frame of every subtype and a running total, so a cap can never again be
    read as a measurement.

  - "Nothing ever clears the queue state byte, so the queue seizes." The byte
    reads zero at every one of six transmit breakpoints, and the driver goes
    on transmitting for the whole run, so it is not seized. A write
    watchpoint on that byte is what said otherwise, three times: QEMU stops
    before the store and continuing re-executes it, so one store reads as
    hundreds of writes from a single program counter. Breakpoints and polls
    agree with each other and disagree with the watchpoint; the watchpoint is
    the one that is wrong.

- **What the ROM does with that byte is now known exactly, and it is a dead
  end rather than the blocker.** Every byte store to offset 18 in the ROM was
  found by decoding the image -- twenty-four sites, eight of them in the WiFi
  code -- and the only values ROM ever writes there are 0, 1 and 3.
  `lmacEndFrameExchangeSequence` clears it only when it already reads 5 or 6,
  and nothing in the ROM ever writes 5 or 6. Those two states belong to the
  flash driver. The routine itself, and all four of its stores, run **not
  once** in a hundred and fifty seconds.

- **The transmit array is one slot wide, and that is measured.** The obvious
  reading -- five slots descending from 0xd08 in eight-byte steps, as this
  family's MAC is usually described -- was tried: the frames the station sent
  fell from dozens to one, because a write to one of the lower offsets is not
  an arming and treating it as one clears bits the driver needs.

- **Reading the frame means reading driver memory**, through the ROM's LMAC
  queue table at 0x3fcef308. That is the same shortcut as reading `wDevCtrl+44`
  for the channel and is called out in the code for the same reason: a real MAC
  fetches the frame by DMA from where the driver pointed it, and this is where
  the driver pointed it, but no wire carries it.

- **And a connection needs more than association.** The WPA2 four-way
  handshake still stands after it, and its AES-CCMP is not in the standard
  library (ADR 0059).
