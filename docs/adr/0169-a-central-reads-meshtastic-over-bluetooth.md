# ADR 0169 — A central reads Meshtastic over Bluetooth

**Status:** Accepted — 2026-11-20. Takes steps 3 and 5 of ADR 0157's road
(L2CAP, ATT and GATT; Meshtastic's own API) and leaves step 4, pairing and
encryption, by the node's own setting.

## Context

With the link transmitting what the controller stages (ADR 0168), a
central's L2CAP frames reach the peripheral's host and its answers come back
in the same descriptors, LLID 2 starting a frame and LLID 1 continuing it.
Measured on 2026-11-20 against Meshtastic 2.7.10 on the T-Deck:

- An ATT MTU exchange: the server offers 255.
- Primary services: `1800`, `1801`, Meshtastic's
  `6ba1b218-15a8-461f-9fa8-5dcae273eafd` at handles 0x000a–0x0014, and the
  Battery service `180f`.
- Meshtastic's characteristics: ToRadio (write), FromRadio (read), FromNum
  (read, notify) and a fourth (`5a3d6e49…`, read, notify).
- A write to ToRadio under the node's pairing mode at the time, FIXED_PIN:
  ATT error 0x05, insufficient authentication, as ADR 0157 read from
  Meshtastic's source.

Two things in our own models stood in the way, and were found on the way:

- **The core advertised at every event, connections' included.** A central
  heard its peripheral advertise all through a connection, which is what a
  peripheral that dropped the link does; a central that reconnects on that
  (as phones do) reconnected for ever. Advertising is now sent on
  advertising events only (activity zero).
- **Nothing retransmits**: the link layer assumes delivery (ADR 0168), and
  one ATT request in a run was lost on its way in. The central's ATT
  transactions are timed out and sent again, as an application's are.

## Decision

1. **`tools/ble_central.py` is the central**: it connects, keeps the link
   with a packet every interval, answers the link layer's feature and
   length requests, reassembles L2CAP frames, and speaks ATT: MTU, services,
   characteristics, read, write. A peripheral advertising again after it was
   connected is a lost link, and the central reconnects. Standard library
   only.
2. **Meshtastic is read without pairing, by its own setting.** Its
   Bluetooth pairing mode is set to NO_PIN through its serial API
   (`tools/mesh_api.py … bluetooth-pairing no-pin`, ADR 0166), as its apps'
   Bluetooth page sets it; the node saves it and restarts, and then reads
   and writes its characteristics on an unencrypted link. Pairing and the
   controller's encryption (ADR 0157, step 4) are not modelled.
3. **Verified as a phone app does it**: write a `want_config_id` to ToRadio
   and read FromRadio until it is empty. The stream that comes back is the
   node's own: channels, configuration sections, module settings, its file
   manifest, its node information, and `config_complete_id` equal to the
   nonce asked with. `tests/test_bluetooth_meshtastic.py` checks it, and
   that the node number over Bluetooth is the one on the cable.

## Consequences

**Good**

- Meshtastic's client API is reachable three ways, as on the device: the
  USB cable (ADR 0166), WiFi (ADR 0164), and Bluetooth.
- Any firmware's GATT server on the S3 can be read and written by a Python
  central.

**Costs, accepted**

- A node set to a PIN cannot be read: pairing is not modelled, and the
  error a phone would see first (insufficient authentication) is what the
  central reports.
- Notifications (FromNum) are not subscribed to; a central polls FromRadio.
- One central per air, and no channel on the frame (ADR 0157, step 2).
- Under `-icount`, which Bluetooth needs (ADR 0027), a configuration stream
  takes minutes of wall time.
