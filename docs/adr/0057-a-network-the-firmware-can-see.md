# ADR 0057 — A network the firmware can see

**Status:** Accepted — 2026-09-10.

**Amended 2026-10-01:** decision 2 is superseded by
[ADR 0110](0110-the-access-point-has-one-channel.md). The access point is on
one channel and its beacons name that one; the station hears it only while
tuned there. `BOARDEMUL_WIFI_CHANNEL` is gone.

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

- **The firmware finds a network, on every scan.** Its own words: with an
  access point beaconing ten times a second, `[WIFI] no networks found` is
  printed **zero** times across twenty-two completed scans. It was printed for
  every scan of every run before today. `no matching wifi found!` remains, and
  should: the network this model advertises is not in the firmware's own list
  of access points to join.

  The rate matters and says something. At one beacon every half second only
  six scans of twenty-seven heard it; at one every tenth of a second -- which
  is what a real access point does -- every scan does. So the model does not
  need to know when the driver is listening, it only needs to beacon like an
  access point.

- **It is a run option, not a hatch.** `--wifi-ap NAME[,MS[,CHANNEL]]` puts an
  access point in the air; the device builds the beacon around the name, so an
  SSID of any length works, and beacons until the run ends. Two thousand
  beacons in a two-hundred-second run, of which the driver took 1987 -- the
  thirteen it missed are moments while it was bringing its station up, and the
  device says so and waits.

- Every step from the interrupt to the scan result is now known and watched:
  the status bit, the ring, the forty-eight-byte header, the length field, the
  channel, the frame's own bytes.

- The path is *robust*: sixty frames delivered, sixty taken; two thousand
  frames at twenty milliseconds, all consumed, the ring re-appended
  throughout, and scanning unaffected.

**Costs, accepted**

- **Two header fields are set to what the code demands, not to what they
  mean.** Word 0's bits 28 and 29 have to have one of them set or the flash
  routine that dispatches on the frame's type goes nowhere; both are set,
  because which one and why is not established. The same is true of the byte
  at +8, which has to be 64, 65 or one of 120 to 127.

- **The Max board gets none of this.** It has no WiFi MAC device modelled at
  all, so `--wifi-ap` there is accepted, warned about, and ignored -- said out
  loud rather than silent, which is ADR 0052's lesson.

- **Nothing transmits while it only scans.** (Corrected by ADR 0060: the scan
  is *active* -- the station sends probe requests. This was measured on a run
  whose transmit queue had already seized, so the probes had stopped.) Breakpoints on eight of the
  ROM's transmit routines fire not once while the station merely scans. But
  given the network the firmware actually wants, it does transmit: see ADR
  0058. This bullet's original claim -- that nothing is transmitted, full stop
  -- was measured on the wrong stimulus and is corrected there.

- Reading `wDevCtrl+44` is a device reaching into driver memory. It is right
  about the channel and wrong in principle, and it is written down as such.
