# ADR 0058 — The firmware tries to connect, and where it stops

**Status:** Accepted — 2026-09-13. Continues ADR 0057; corrects its last cost.
**Corrected the same day by ADR 0059:** the claim below that `lmacTxFrame`
never runs is an artefact of attaching a minute into the boot. It runs six
times at nine and a half seconds, and the queue is busy afterwards because
nothing completed those six frames -- which is what ADR 0059 models.

## Context

ADR 0057 ended with "nothing associates, and nothing is transmitted", and said
that was measured: eight transmit routines broke not once while the station
scanned. That was true, and it was also the wrong test. A scan is passive
because the firmware has no reason to speak -- it has not been given a network
it wants. Give it one, and it speaks.

**The firmware's own network name is in its image.** Among its strings, beside
`wifi`, `LilyGo-AABB` and an NTP server, is `xinyuandianzi` -- the SSID this
build's `WiFiMulti` is compiled to join, with `AA15994823428` beside it as the
passphrase. So the way to make the firmware act is to advertise that name.

**Advertising it moves the verdict, in two steps.**

- An *open* network called `xinyuandianzi`: the scan finds it, and the firmware
  changes its complaint from `no matching wifi found!` to
  `Connecting Failed AP not found.` It wants the network now -- it just cannot
  find it at connect time. A station given a passphrase runs its connect-time
  scan filtered to networks that *have* one, and an open beacon is filtered
  out. Every scan still lists it; the connect scan does not.

- The same name with `--wifi-wpa2`, which adds the privacy bit and an RSN
  element for WPA2-PSK/CCMP to the beacon: the verdict becomes
  `Connecting Failed.` -- no longer "not found". The station finds the access
  point, decides it is the one, and begins to join.

**And then it builds a frame.** Broken at `ieee80211_set_tx_desc` and
single-stepped, the connect attempt runs the whole output path: it allocates a
transmit buffer (`esf_buf_alloc`), builds the 802.11 descriptor, reads its own
MAC address, sets the transmit power-index and rate through `ieee80211_set_tx_pti`
and `rcGetSched`, computes the frame's on-air time (`ppCalFrameTimes`), and
maps it to a hardware queue (`ppMapTxQueue`). The frame is real, addressed, and
rate-selected.

**Where it stops is one routine, and it is named.** `ppTxProtoProc` at +349
calls `lmacIsIdle` and, on a zero return, jumps to +443, which returns 0 and
abandons the send:

    a10 = lmacIsIdle(queue)
    if (a10 == 0) return 0        // nothing queued to the MAC

`lmacIsIdle` reads byte 18 of the queue's entry in the LMAC queue table
(`0x3fcef308`, the literal `lmacTxFrame` and `lmacEndFrameExchangeSequence`
also write). That byte is the queue's transmit state, and it is only ever
advanced by the MAC actually running a frame through: `lmacTxFrame` sets it as
it hands a frame to hardware, and `lmacEndFrameExchangeSequence` clears it when
the exchange completes. Neither runs here, because nothing drives the transmit
engine -- so the queue never leaves the state that makes `lmacIsIdle` say "not
idle", and every attempt bails at the same branch.

`lmacTxFrame` breaking not once across two hundred seconds of connect attempts
confirms it: the frame reaches the queue and never reaches the MAC.

## Decision

1. **Advertise the network the firmware wants, and say how it is secured.**
   `--wifi-ap xinyuandianzi --wifi-wpa2` is the shortest path to making the
   firmware try to connect, and the beacon now carries the RSN element a
   secured station looks for. This is a run option, tested only by hand so far.

2. **Stop at the transmit engine, and do not guess it.** What remains is a
   whole subsystem, not a field: the MAC would have to accept a queued frame,
   advance the queue-state byte, carry the frame to an air, raise a
   transmit-complete interrupt, and let `lmacEndFrameExchangeSequence` clear
   the queue -- and then answer the authentication and association frames the
   firmware is trying to exchange, which is a two-sided protocol. Building that
   speculatively is the failure mode ADRs 0048 and 0050 cost three documents
   to. It is the next piece of work, recorded with its starting point, not
   half-built.

## Consequences

**Good**

- **The receive half is complete and the connect intent is proven.** The
  firmware finds the network, selects it, builds an encrypted association
  frame, rate-selects it and queues it. Everything up to the radio actually
  transmitting is exercised.

- The boundary is a single named branch (`ppTxProtoProc+349` on `lmacIsIdle`),
  and the state it turns on is a single named byte in a located table. The next
  investigation starts there, not from "nothing transmits".

- Two more receive gates were found on the way and are modelled: the MAC-hold
  bits at `+0xca0` (`0x00ff1000`, set by `ic_mac_deinit` and cleared by
  `ic_mac_init` around every channel change) and the receive-enable bit at
  `+0x0d8` (bit 8, toggled by the driver around each reconfiguration). A beacon
  delivered while either said "not listening" was the intermittent dead-ring
  that ADR 0057's "about half the time" was really describing: it was not the
  scan window, it was a frame landing during bring-up, being acknowledged
  unread, and leaving the ring full and un-rearmed. Since the gates, a frame is
  only delivered when the MAC is actually up, and the ring no longer dies.

**Costs, accepted**

- **No connection.** The station tries and fails, which is further than "finds
  a network" and short of "joins" one. A transmit engine and a two-sided
  auth/assoc exchange are what stand between.

- The SSID and passphrase are read from the image and given on the command
  line. Nothing here reads the firmware's stored config to discover them
  automatically; a run that wants a connect attempt has to be told the name.

- The transmit path was read by single-stepping one association attempt, so
  the account is of that one attempt. A different security mode or a different
  firmware would take a different path from `ppTxProtoProc` onward.
