# ADR 0109 — The air acknowledges, retries, and holds frames for a dozing station

**Status:** Accepted — 2026-10-01. Closes item 6 of ADR 0106. Revisits ADR
0067's "the air does not buffer" for unicast frames, and fills the place ADR
0062 left for a real acknowledgement timeout.

## Context

ADR 0106 recorded that the WiFi MAC model acknowledges nothing at the 802.11
level. Measured, before changing anything, over one 200-second WPA2 session of
the vendor's 250620 build (join, handshake, DHCP, DNS, NTP, a TCP
registration):

- **Every lost unicast frame from the access point was handshake or data.**
  5,348 frames met a receiver that was off and 182 a held MAC, all of them
  broadcast beacons, and losing those is right. But two EAPOL message ones and
  one IPv4 frame met a full receive ring and were dropped. The handshake
  completed only because the peer sent message one again
  (`eapol 1 again ×2`).
- **The station uses 802.11 power save.** Of its frames, 7 null-data frames
  carried the power-management bit set and 6 clear, and 13 probe requests went
  out with it set. That is how a station tells its access point "I am going
  off-channel to scan, keep my frames", and "I am back". The access point
  ignored the bit.
- **Our beacon had no TIM element.** Every real beacon carries one: the DTIM
  count and period, and the bitmap of stations with frames buffered for them.
  Every station was also given association ID 1.
- **Every frame the station transmitted completed as acknowledged.** ADR 0062
  decoded the result word the driver reads and wrote success on purpose,
  noting that "a real acknowledgement timeout ... has a place to be written".
  The same path also reported success for a frame the device had declined to
  hand to the access point because an earlier answer was still pending.

This is what 802.11 (IEEE Std 802.11-2016, clauses 10 and 11.2) does instead,
for a 2.4 GHz OFDM network:

- An individually addressed frame is acknowledged by its receiver. Without the
  acknowledgement the sender retries, with the Retry bit set, after an ACK
  timeout (SIFS + slot + PHY start delay: 10 + 9 + 25 µs) and DIFS (28 µs)
  plus a backoff drawn from a contention window that starts at 15 slots and
  doubles to 1023, up to `dot11ShortRetryLimit` (7) attempts. Then it gives
  up and reports the failure upward. Group-addressed frames are sent once and
  never acknowledged.
- A station's power-management bit, in any frame it sends, says whether it
  will be dozing after the exchange. The access point buffers individually
  addressed frames for a dozing station and sets that station's bit in the
  TIM of its beacons. The station gets them by switching back to active mode
  (a frame with the bit clear), or one at a time with a PS-Poll. Each frame
  delivered to a dozing station has More Data set while more are buffered. A
  PS-Poll that finds nothing buffered is answered with a null-data frame.

On the device side, ADR 0062's table is how the driver is told: result bits
12..15 of 5 send it to `lmacProcessAckTimeout`.

## Decision

1. **A station's frame is acknowledged only if the access point heard it.**
   The peer replies with a new flag, `REPLY_UNACKED`, for an individually
   addressed frame that is not addressed to it. The device then completes that
   slot with result 5 (ACK timeout), and the driver's own retry logic takes
   over. Group-addressed frames complete as success, as on the air.

2. **No transmitted frame is swallowed.** The device keeps a small queue of
   answers (four) behind the one being delivered. A frame the station sends
   while an earlier answer is pending still reaches the access point, which is
   what its radio would do.

3. **The access point's radio retries what the station did not take.** An
   individually addressed frame that meets a held MAC, a receiver that is off,
   a full receive ring or a descriptor with no buffer is retried with the Retry
   bit set. Each retry waits the ACK timeout, DIFS and the *mean* backoff for
   its attempt, so the timing is deterministic: about 9.5 ms over seven
   attempts. After the seventh the frame is dropped and the peer is told, with
   a new request flag `FRAME_UNACKED`, as hostapd is told by a failed
   `tx_status`. Answers keep their exemption from the receive-enable gate
   (ADRs 0062, 0068), because they reach the receiver before the driver closes
   it, but they now obey the same retry limit at the other gates. Beacons go
   once.

   Whether the ESP32's MAC acknowledges a frame it has no receive buffer for
   is not documented anywhere read here. Treating a full ring like a deaf radio
   is a choice. Its cost is that a frame a real station might have
   acknowledged and lost is delivered a few milliseconds late instead.

4. **The access point does power save.**
   - Each station gets its own association ID.
   - Beacons, and only beacons, carry a TIM: DTIM period 1, with the bit of
     every station that has frames buffered. Probe responses carry none, as
     the standard says.
   - The power-management bit of each frame a station sends sets whether it is
     dozing.
   - Frames for a dozing station are held, in order, up to 64: data frames
     and the handshake's messages alike. The answers to probes,
     authentications and associations are never held, because they go to
     stations that are not associated or are just waking. A frame with the bit
     clear wakes the station, and its held frames go out first. A PS-Poll is
     answered with one held frame, More Data set while more remain, or with a
     null-data frame when none do.
   - The access point sends no group-addressed data, only beacons, so nothing
     group-addressed is buffered. That would change if it ever did.

## Consequences

**Good**

- A frame is lost only where it would be on the air: a beacon the station's
  radio missed, or a unicast frame that seven attempts could not land.
- A station going off-channel to scan stops costing it frames, because the
  access point holds them, as the station asked.
- The driver's acknowledgement-timeout path, which ADR 0062 named and nothing
  ever drove, is now reachable from an honest cause.

**Costs, accepted**

- The retry timing uses mean backoffs rather than random draws, so runs stay
  reproducible. Contention between stations, which a random draw exists to
  resolve, is not modelled; there is one station.
- The full-ring choice in decision 3.
- The access point still answers on whatever channel the station is tuned to,
  as ADR 0057 arranged so that a beacon claims the channel it arrived on. A
  real access point is heard on its own channel only. That is a separate
  inaccuracy, recorded here rather than mixed into this change.
