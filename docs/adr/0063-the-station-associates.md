# ADR 0063 — The station associates

**Status:** Accepted — 2026-09-15. Answers the question ADR 0062 left open, and
corrects its last guess about where the answer was.

## Context

ADR 0062 established that the authentication reply reaches
`wDev_IndicateFrame` and is handed to `wDev_DiscardFrame`, that the gate is a
second argument tested against one, and that the reply arrived with a 4 while a
beacon arrived with a 1. It then guessed the argument was a queue, looked for
it in the driver's IRAM caller, and said the next step was to trace that.

**The caller was the wrong one and the argument is not a queue.** At the
breakpoint the return address in `a0` reads 0x4212159e, which is in `ppTask`;
but a0 before the callee's `entry` executes holds the return address one level
*up*, a trap this project has recorded before. The level in between is
`wdevProcessRxSucDataAll` (0x4004147c), which is in ROM and therefore readable
without running anything at all:

    a3 = 0
    loop: word = *desc ; a3 += 1 ; next = *(desc + 8)
          if (!(word & (1 << 30))) goto skip      -- no frame in this one
          wDev_ProcessRxSucData(desc, a3) ; a3 = 0
    skip: desc = next ; if (desc) goto loop

So the second argument is **how many descriptors were examined to reach this
frame**. One means "the next one in order". Anything else means descriptors
were passed over, which the ROM reads as frames lost, and `wDev_IndicateFrame`
throws the frame away rather than give the stack a gap it cannot account for.
Bit 30 is `DESC_EOF`, which this model already sets; the count was the part
nobody was setting.

**And that is a defect in this model, not in the firmware.** The device
delivered into the first free descriptor starting from whichever one the driver
had most recently handed back, so a frame landed at an arbitrary position in
the list the consumer walks. A real MAC does not choose: it writes into the
next descriptor in order, and if that one is still full it is out of room.

The consequence had been visible for two days without being legible. A scan
survives an arbitrary position, because beacons arrive many a second and
enough of them happen to land first -- which is why the scan was never
reliable either, fourteen of nineteen and never all. A single authentication
reply gets one throw of the same dice, and lost it nearly every time.

## Decision

1. **Deliver into the head of the list the consumer walks.** `*wDevCtrl` is
   where `wdevProcessRxSucDataAll` starts, so it is where a frame has to be for
   the count to come out at one. If the head is not free, nothing is delivered
   and the frame is lost, which is what a real MAC does when it has run out of
   room -- rather than putting it somewhere the driver will discard.

   Reading `wDevCtrl` is the shortcut ADR 0057 already takes for the channel,
   for the same reason and with the same note beside it.

2. **The behaviour it replaces is kept, on a switch.**
   `BOARDEMUL_WIFI_ANYDESC=1` restores the old walk whole. The difference is
   the sort of thing that gets argued about rather than measured, and one run
   each settles it.

3. **The new capability gets a test**, `tests/test_wifi_connect.py`: the
   authentication frame and its reply, the association request the station
   only sends because that reply was accepted, and the response to it -- with
   the ordering asserted, so a run that somehow associated first would be
   caught rather than believed.

## Consequences

**Good**

- **The station associates.** One run each, nothing changed but the walk:

  | walk starts at | authentications | association requests | silent scans |
  | --- | --- | --- | --- |
  | the list head | 5 | **5** | 0 of 9 |
  | the last freed descriptor | 7 | **0** | 1 of 13 |

  The exchange in full, from the model's own log: the station authenticates,
  the reply goes back a millisecond later, and four milliseconds after that
  the station asks to associate and is accepted. That is the first time either
  board has got past authentication.

- **The scan is reliable now too**, which was not the aim. Every scan in the
  run heard the network, where the best before was fourteen in nineteen. Same
  cause: frames were being discarded for being out of order, and a beacon that
  is discarded is a beacon the scan did not hear.

- The receive path is now understood end to end, from the interrupt bit
  through the descriptor list to the frame reaching the stack, with the
  ordering rule written down rather than stumbled into.

**Costs, accepted**

- **It stops at the four-way handshake, about a second after associating.**
  The station disassociates and the firmware reports `Connecting Failed`,
  which is what a WPA2 handshake timing out looks like. Getting past it needs
  AES-CCMP, which is not in the standard library, and the passphrase. That
  wall was named in ADR 0059 and has not moved; what has changed is that
  everything in front of it is now done.

- **A beacon rate that a scan can hear is part of the configuration.** At a
  tenth of a second the firmware scans in short bursts while it retries and
  most scans hear nothing, so the first association took two minutes; at
  twenty milliseconds none of them miss and it takes forty-eight seconds. The
  test asks for twenty. A real access point beacons at about a tenth of a
  second, so this is the model buying time the firmware would otherwise spend
  waiting, and it is written into the test rather than into the device.

- **A slow test's run directory has to be on a local filesystem.**
  `pytest.ini` puts it under `logs/` so artefacts survive, which is right for
  every other test and wrong for this one: on a checkout mounted into the
  build container, the panel frames and logs written across the mount cost
  enough to change the guest's timing. Measured, the same command with nothing
  changed but the output directory -- nine scans and none silent writing
  locally, **seventy-four scans and seventy-one silent** across the mount, and
  no connection at all. So this fixture runs in a scratch directory and copies
  the two logs it needs back out. It is the first test in this suite whose
  result depends on where it writes, and it will not be the last.

- **`tools/disas_rom.py` was wrong about branch immediates** until this work
  found it, and the wrong reading is what sent ADR 0062 after byte 47. A BI0
  branch's four-bit field is an index into `B4CONST`; indices one to eight are
  their own value, so it read correctly for years and then reported a
  comparison against 13. Fixed separately.
