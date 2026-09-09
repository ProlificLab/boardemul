# ADR 0055 — A frame the WiFi driver accepts

**Status:** Accepted — 2026-09-09. Takes down most of the wall ADR 0054 named.

## Context

ADR 0054 located the receive ring, read the descriptor's layout off a constant,
and said two things could not be known from a run in which no frame ever
arrives: which interrupt announces one, and what the driver does when it wakes.
Both are now known, and neither was guessed -- each came from raising something
and watching what the driver went and read next, which is the same method the
Bluetooth receive path was found by (ADR 0046).

**The interrupt does arrive.** `-d int` shows a pending bit the run never has
otherwise: `intset = 00000001`, only ever after a delivery. So the WiFi MAC's
own source reaches the processor and the handler runs. It just had nothing to
do, which is a different problem and was worth separating.

**A frame is not announced by the interrupt.** Woken with nothing to say, the
handler reads `+0xc3c`, finds zero, and returns -- then comes straight back,
because the line is still up. That register is the status. Answering it, the
handler acknowledges by writing `+0xc40`, not by writing the status back, which
is the ordinary split between a status and its acknowledgement.

**Then it asks three more things**, each found the same way, by answering the
last one and seeing what it read next:

- `+0x084`, beside the ring: it sets bit 0 and waits for the hardware to clear
  it. The same self-clearing shape as the reset gate at `+0xd14` (ADR 0019).
  There is no radio here to keep it busy, so the command is taken at once.
- `+0x090`, always read straight after the memory base at `+0xc64`: where the
  hardware got to. Reading nothing, the driver asks again forever. Told which
  descriptor was filled, it stops asking.
- and then it takes the descriptor.

**Taken means taken.** The descriptor the model filled reads `0x808a48a4`
again afterwards -- owner set, length back to the buffer's size. The driver
processed it and handed it back, which is what a driver does with a frame
whether or not it made anything of the contents.

## Decision

1. **Model the handshake, not the outcome.** Status, acknowledgement, the
   self-clearing command and the position readback are all things the hardware
   does on its own account, and each was watched rather than assumed. What the
   driver then makes of the bytes is its business.

2. **The status bits are narrowed, not pinned.** Raising all thirty-two makes
   the driver take a different path and drop the frame; the low half works and
   `0x0000ff00` works. Which single bit means "a frame arrived" is not
   established, so `BOARDEMUL_WIFI_STATUS` sets what is raised rather than the
   model claiming to know.

3. **The hatch stays a hatch.** Nothing is delivered unless
   `BOARDEMUL_WIFI_FRAME` asks for it. With it unset the device is the window
   it replaced: the firmware brings its station up and scans exactly as before.

## Consequences

**Good**

- **The driver accepts a frame from the model.** Every gate between an
  interrupt and a consumed descriptor is answered, and the ring turns over.
- Four registers are named that were not: the status, its acknowledgement, the
  receive command and the position. All are in the page the model already owns.
- The method generalises and cost one run each: answer what the driver last
  read, and it tells you what it wants next by reading it.

**Costs, accepted**

- **A scan still finds nothing.** The driver takes the frame and makes nothing
  of it, which points at the buffer's contents rather than at the handshake.

  That is now a much smaller search than it was. Because the driver reacts, the
  code that reads the frame runs, and diffing the blocks a run translates with
  a frame against one without gives **forty-five**, in four tight clusters.

  One of them is a field accessor -- given a number it returns one of three
  things -- and resolving its literals says where those things live. Not in the
  buffer, which is what a first reading of it suggested: they are registers in
  this same page. The frame's **length is the low eleven bits of `+0xca8`**,
  bits 16..23 of the same register are a second field, and the low four of
  `+0xcb0` are a third.

  Reading zero for the length is why the driver first took the frame and made
  nothing of it. Told the length, it reads it back as the fifty-six bytes that
  were delivered, checks `+0xcf0`, and discards the frame anyway -- and asks
  for nothing else. So what remains is in the bytes themselves, and the two
  fields not yet supplied are where to start.
- The status mask is a range, not a bit.
- One frame is delivered, once. A scan sweeps channels and would want a beacon
  on the right one at the right moment, which needs the air the radio and
  Bluetooth each have.

## Alternatives rejected

**Keep sweeping interrupt sources.** Three more would have been wasted: the
source was right from the start and the silence was the status register.

**Set the whole status word and take the result.** It makes the driver drop the
frame, which looks like failure and is a different branch being taken. The
halves were worth the two runs.
