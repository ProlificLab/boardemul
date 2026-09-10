# ADR 0057 — A network the firmware can see

**Status:** Accepted — 2026-09-10.

## Context

ADR 0056 got a delivered frame all the way into the flash 802.11 stack and left
it there: parsed as a beacon, addresses read, and then recycled without
becoming a scan result. Three more things were wrong with the frame, and each
was found the same way -- a breakpoint, a few hundred single steps, and the
decoded instruction printed beside each one, so a branch's condition is read
off its own line rather than guessed at.

**The length has to be in the header, twice.** Word 1 bits 8..19 is the field
`wDev_ProcessRxSucData` copies into the low twelve bits of the word at +44.
The flash routine that parses the frame reads +44, and drops anything under
sixteen bytes -- and the ROM only performs that copy on one of its two
branches, not the one this model's frames take. With +44 left at zero the
frame was recycled unread; with the length written there the same frame is
parsed as a beacon, protocol version checked, transmitter address compared
against the station's own, subtype recognised as 8, and its information
elements walked one by one.

**And then the channel.** At 0x420c5998:

    a10 = the beacon's own channel, from its DS Parameter Set element
    a9  = the channel the frame arrived on
    if (a9 == a10) accept
    if (*(g_ic + 116)) ... else return -1

A beacon that claims a different channel from the one it arrived on is thrown
away, unless a flag in `g_ic` says otherwise, and that flag is zero. This
model's beacon said "channel 1" always, because the array it is built from
says so; the frame arrives on whatever channel the scan is dwelling on. They
almost never agreed.

The channel a frame arrives on is not something this device could see. The
radio's channel is programmed through the front-end windows, which this project
models in Python, and this device is in C with no view of them. But the ROM's
own receive path faces the same problem and solves it by reading `wDevCtrl+44`
-- so that byte *is* the controller's notion of the channel it is tuned to,
and the model reads the same byte.

## Decision

1. **The model writes the length where the stack reads it**, in both places,
   and the count includes the check sequence -- which is what the descriptor's
   own length field says arrived.

2. **A beacon claims the channel it arrives on.** The number comes from
   `wDevCtrl+44`, the same byte the ROM reads for the same purpose.
   `BOARDEMUL_WIFI_CHANNEL` pins it, for the run that wants to see the
   mismatch.

3. **Reading driver memory from a device is called out, not hidden.** It is a
   shortcut. The clean version routes the channel from the Python radio windows
   into this device, and that wants the channel encoding those registers use,
   which is not yet decoded (ADR 0054 names the registers, not their contents).
   Until then the shortcut is documented in the code, beside the address it
   reads.

## Consequences

**Good**

- **The firmware finds a network.** Its own words: with beacons arriving,
  `[WIFI] no networks found` stops being printed for scans that heard one --
  six of twenty-seven at one beacon every half second, fourteen of
  twenty-four at one every tenth of a second. `no matching wifi found!`
  remains, and should: the network this model advertises is not in the
  firmware's own list of access points to join.

- Every step from the interrupt to the scan result is now known and watched:
  the status bit, the ring, the forty-eight-byte header, the length field, the
  channel, the frame's own bytes.

- The path is *robust*: sixty frames delivered, sixty taken; two thousand
  frames at twenty milliseconds, all consumed, the ring re-appended
  throughout, and scanning unaffected.

**Costs, accepted**

- **A beacon lands in a scan's collection window about half the time**, so a
  scan sometimes still reports nothing. A real access point beacons every
  hundred milliseconds on its own channel and a station dwells long enough to
  hear it; this model delivers on the channel the driver is tuned to, which is
  not the same thing as delivering when it is listening. What decides the
  other half is not yet known.

- The frame is still a fixed beacon behind a hatch. An access point in the air
  -- an SSID, a channel, a beacon interval, a run that says one is there --
  is a separate step, and the next one.

- Reading `wDevCtrl+44` is a device reaching into driver memory. It is right
  about the channel and wrong in principle, and it is written down as such.

- The frame is not associated with, and nothing is transmitted. Finding a
  network is the first thing WiFi can do, not the last.
