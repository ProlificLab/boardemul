# ADR 0095 — A delivered message one is not retried at once

**Status:** Accepted — 2026-09-30. Chases the flake ADR 0094 left on record:
`test_wifi_connect`'s "completed the four-way handshake" failed about one run
in three. Two hypotheses were tested and falsified before the cause was found;
both are kept here because the instruments they produced stay.

## Context

In a failing run the firmware still gets an address, but the peer never counts
a message four, so it never hands the MAC the key (ADR 0094) and the run falls
back to the identity cipher. The peer's own counts showed message four never
reached `Handshake.take`: every signed frame after the keys exist is counted as
message four, as a message two that did not verify, or as a message two that
did, and only the last appeared.

The flake stopped reproducing once WSL was repaired: ten clean runs, then six
under three-way load, all passed, and none had a duplicate message two. It came
back deterministically with the peer's message-one retry shortened
(`BOARDEMUL_WIFI_FIRST_RETRY_MS=5`, a new experiment knob; default unchanged):
three runs out of three failed with the original signature.

**Hypothesis one, falsified: the radio drops message four.** `wifi_tx_complete`
does not forward a transmitted frame while an earlier answer is still waiting
to be delivered, and it said nothing when it did so. That exit now logs the
frame it drops, with its EAPOL key information. Across seventeen runs, forced or
not, it never fired.

**Hypothesis two, falsified: several message threes confuse the station.** The
peer answered every message two with message three, where hostapd drops a
message two that arrives after it has sent message three. Making the peer drop
them left one message three per run, and message four was still missing.

**What the air shows.** The device now logs every data frame the driver puts on
slot 0, with its ethertype and EAPOL key information, and says when an armed
frame cannot be read back. It already watched slots 1–7 for arms it does not
service. In every failing run the station transmits message two several times,
then DHCP, a gratuitous ARP and a keepalive, **and never a message four on any
slot**. Its receive descriptors show why there are several message twos: three
copies of message one were delivered to it (at 155812, 155832 and 155905 ms)
before it answered the first, 118 ms after it arrived, because deriving the
keys is slow in emulation. The peer retried message one every 100 ms (5 ms
forced) whether or not the last copy had arrived, because it could not tell.
The clean runs are the same exchange with one message one delivered, and in
them message four is always sent, straight after the single message two.

Why the station then skips message four, while still completing the handshake,
is inside its supplicant and not visible from here. That supplicants mishandle
exactly this is documented: hostapd's `wpa_auth_eapol_key_tx_status()` extends
the message-one timeout once the frame is acknowledged, because some deployed
supplicants break the handshake "whenever the relatively short initial timeout
is reached and EAPOL-Key 1/4 is retransmitted".

## Decision

**Tell the peer when a frame it offered was delivered, and let it do what
hostapd does with that.** The device sets a bit (0x8000) in the type field of
its next request when the last frame the peer offered on a tick actually
reached a receive descriptor -- the model's version of a transmit
acknowledgement. The peer masks it off, and if that frame was a message one,
retries message one after 1000 ms (hostapd's `eapol_key_timeout_subseq`)
instead of 100 ms. A message one that was not delivered is still retried at
100 ms: this radio loses offers the air would not (ADR 0067), and that cadence
is what gets the handshake started at all.

The instruments stay: the per-frame transmit log, the two formerly silent exits,
and the retry knob.

## Consequences

**Good**

- The station receives message one once, as from a real access point that has
  heard its acknowledgement, and answers with one message two, one message
  three and a message four -- under the same forced condition that failed three
  times in three.
- The peer's retry now depends on what the air did rather than on a timer
  alone, which is the information a real authenticator has.

**Costs, accepted**

- The request header grows a flag, carried in the high bit of a field that uses
  two values. Both ends are ours and change together.
- **The mechanism inside the station is not understood**, only its trigger. The
  fix removes the trigger in the way the reference authenticator does. If a
  future firmware skips message four for another reason, the per-frame log will
  show it the same way.

## Verification

In the container, three runs at a time:

| Condition | Before | After |
|---|---|---|
| Forced, `FIRST_RETRY_MS=5` | 3/3 failed, twice; 3/3 with duplicate message twos dropped | 3/3 passed |
| Default | 10/10 passed (healthy host; no duplicates) | 3/3 passed |
| `test_wifi_scan` | — | passed |

Every passing run after the change shows one message one delivered before the
first message two, the peer logging `eapol 1 delivered`, message four on slot 0
(135 bytes, key_info `0x030a`), the key handed to the MAC, and no frame failing
its MIC.

One batch of three forced runs, started together straight after a rebuild,
crashed during boot instead -- `Stack canary watchpoint triggered (ipc1)` on
core 1 at the same PC in all three, before WiFi had handed over its receive
ring, so before anything here runs. The same batch rerun unchanged passed three
out of three. Recorded as a separate, unexplained boot-time observation under
`thread=multi`, not as a result of this change.

## Alternatives rejected

**Drop a transmit less, or queue answers behind a pending one.** The drop never
fired; it would have fixed nothing.

**Drop duplicate message twos, as hostapd does.** Tried, and message four was
still missing. It is not needed once message one is not duplicated.

**Make the test tolerate a missing message four.** The firmware would still be
running on the identity cipher in those runs, which is the failure, not noise.
