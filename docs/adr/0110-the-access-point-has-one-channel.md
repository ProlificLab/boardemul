# ADR 0110 — The access point has one channel

**Status:** Accepted — 2026-10-01. Supersedes decision 2 of ADR 0057. Closes
the inaccuracy ADR 0109 recorded.

## Context

ADR 0057 found that the driver throws away a beacon whose DS Parameter Set
names a channel other than the one it arrived on. It also found where that
channel comes from: the ROM stamps a received frame with `wDevCtrl+44`, the
controller's notion of the channel it is tuned to. Its fix was to have the
beacon claim whatever channel that byte said. Scanning then worked.

But the fix also made the access point exist on every channel at once. The
peer took the station's channel as its own on every request
(`self.channel = channel or self.channel`), so:

- a scanning station heard the network on every channel it dwelt on, all
  thirteen, where a real access point is heard on one;
- probe requests were answered on every channel;
- a station could exchange frames with it wherever it was tuned. That
  includes in the middle of a scan, while it had told the access point it was
  away (ADR 0109).

`--wifi-ap NAME,MS,CHANNEL` already had a channel field. It defaulted to
"whichever the controller is tuned to", and given explicitly it only changed
what the beacon claimed, not where it could be heard.

What makes the honest model possible is the same byte ADR 0057 reads. The
device already sends the station's channel with every request. It only has to
be compared with the access point's.

## Decision

1. **The access point is on one channel**: the one `--wifi-ap` gives, or 1
   by default, which is the default in hostapd's sample configuration. The
   "whichever the controller is tuned to" mode is gone. Its beacons and probe
   responses name that channel in their DS Parameter Set.

2. **It hears only frames sent on its channel.** A station frame that arrives
   with any other channel was sent where the access point is not. It is not
   answered and changes nothing, and if it was individually addressed, it is
   not acknowledged: the driver sees an acknowledgement timeout (ADR 0109).

3. **The station hears it only while tuned to that channel.** The device
   delivers a frame only when `wDevCtrl+44` says the station is on the access
   point's channel. Anything else is "not taken" under ADR 0109: a beacon is
   missed, and an individually addressed frame is retried and then given up.
   Nothing is exempt. An answer or an awaited frame cannot arrive on a
   channel its sender is not on.

4. **`BOARDEMUL_WIFI_CHANNEL` is removed.** It pinned the device's reading
   of the station's channel "for the run that wants to see the mismatch". The
   mismatch is now physical and visible in every run.

5. **Adjacent channels are not modelled.** On 2.4 GHz a receiver tuned near
   the access point's channel can decode some of its frames. For beacons that
   changes nothing the driver keeps, because it discards a beacon whose
   channel disagrees with where it arrived (ADR 0057). For anything else, an
   associated station is on the access point's channel.

## Consequences

**Good**

- A scan finds the network on its own channel, and only there. A station
  joins on that channel, and while it scans elsewhere the access point
  neither hears it nor reaches it.
- The device and the peer agree on one fact, the access point's channel, and
  neither guesses.

**Costs, accepted**

- A scan can miss the network if no beacon or probe answer lands inside the
  station's dwell on the access point's channel, as on the air. ADR 0057
  measured how much the beacon rate matters for that, and the suites keep
  their rates.
- Every run's air changes, including those that never gave a channel: the
  access point is on channel 1 now, not everywhere.
- A scanning station is on the access point's channel for a fraction of each
  sweep, so far fewer frames reach it. The device's progress line used to come
  every hundred deliveries, which a scan run could now outlast. It comes every
  hundred frames the access point sends instead, heard or lost.
