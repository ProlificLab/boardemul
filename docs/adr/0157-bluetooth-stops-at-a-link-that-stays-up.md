# ADR 0157 — Bluetooth stops at a link that stays up, and this is the road past it

**Status:** Accepted — 2026-11-18. Decides where the Bluetooth model stops
for now, after ADRs 0044–0051. Steps 1, 3 and 5 taken by ADRs 0168 and
0169, step 4 by ADR 0171; step 2, channel hopping, remains.

## Context

ADRs 0044 to 0051 took the ESP32-S3's Bluetooth controller from a link layer
that ran and sent nothing to one that advertises on an air of its own,
accepts a central's connection request, and keeps the connection up at the
interval it agreed. ADR 0051 ends there: the packets are empty ones,
`r_lld_con_rx_llcp_check` and `r_lld_con_data_tx` never run, the medium has
no channels, and nothing is carried.

The census of 2026-11-18 put "Bluetooth carries no data" at the top of what
is open, because a phone talks to Meshtastic over it. What carrying data
would take, layer by layer, from what this project has read:

1. **The link layer's control procedures.** A central sends control PDUs,
   the version exchange, features, the data length, and the controller must
   answer them. The receive half exists: our core hands the controller a
   packet at the event it was due (ADRs 0049, 0050). The transmit half does
   not. What the controller stages to send in a connection event lives in
   its exchange memory in a layout of the ROM's own, which no published
   source describes; advertising was read off the controller's mappings
   (ADR 0045), and a data descriptor would have to be read the same way.
2. **Channel hopping**, so that a packet is heard on the channel its event
   is on (ADR 0051).
3. **L2CAP, ATT and GATT** on the central's side: a Python peer that
   discovers services, reads, writes and subscribes. Published, and long.
4. **Pairing and encryption.** Meshtastic's characteristics are read and
   written encrypted and authenticated unless its Bluetooth mode is `NO_PIN`
   (`src/nimble/NimbleBluetooth.cpp`: `PROPERTY_READ_ENC`,
   `PROPERTY_READ_AUTHEN`, `PROPERTY_WRITE_ENC`). An encrypted link is
   AES-CCM done by the controller's hardware, which our core would have to
   model, and pairing is the Security Manager's exchange on top.
5. **Meshtastic's own API** over those characteristics: protobufs, through
   the standard library only (the project's rule).

Each of these is a record of its own, of the size of ADRs 0044–0051 together
for the first, and the second and fourth are hardware this project would
have to read off the ROM rather than from a document.

## Decision

1. **The Bluetooth model stops at a link that stays up**: advertising,
   connection, connection events at their interval, on both boards (ADR
   0148). It is stated as such, in the README and here, not as work in
   progress.

2. **The road past it is the five steps above, in that order.** The first
   is the one that decides whether the rest is reachable: whether the
   controller's transmit descriptors can be read as faithfully as its
   advertising records were.

3. **What a person can do over Bluetooth meanwhile is see it**: a Meshtastic
   node advertises its name on `--ble-air`, and a central can connect.

## Consequences

**Good**

- The Bluetooth gap is a boundary with a map, not an open question.

**Costs, accepted**

- A phone cannot configure an emulated Meshtastic node over Bluetooth. Its
  setup goes through its own interface (ADR 0026), and its serial ports take
  what is typed into them (ADR 0156).
