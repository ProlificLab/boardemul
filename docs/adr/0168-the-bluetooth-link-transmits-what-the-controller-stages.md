# ADR 0168 — The Bluetooth link transmits what the controller stages

**Status:** Accepted — 2026-11-20. Takes the first step of the road ADR 0157
laid out: the link layer's control procedures.

## Context

ADR 0051 left a connection that stays up and carries nothing: the core
delivers what a central sends, and never sends what the peripheral has
staged, because where the controller stages a packet to transmit was not
known. ADR 0157 named finding it as the step that decides whether the rest
of the road is reachable.

It was read on 2026-11-20 from the ROM and from what the controller writes,
with a hatch that logs every transmit descriptor and each link's control
structure as they change (`BOARDEMUL_BLE_TXPEEK`), on a Meshtastic node with
Bluetooth on and a central connected.

- **The transmit descriptors** are 14-byte entries at exchange-memory offset
  0x1400: `r_lld_con_tx_prog` (ROM 0x4001afe8) indexes them from its literal
  at 0x40014c08, which is 0x1400. The advertising set uses entries 0 and 1, a
  ring of two; link 1 uses entries 9 and 10, likewise.
- **An entry** is, as halfwords: +0 the next entry's offset in the low
  fifteen bits and, in bit 15, *done*; +2 the PDU's two header octets
  (LLID, NESN, SN, MD; then the length); +4 the payload's offset. Seen: link
  1's entry 9 at `148c 0913 1c00`, pending, its next entry 10 at `947e`,
  done, pointing back; its payload `0e ff f9 01 00 …` is
  LL_PERIPHERAL_FEATURE_REQ (opcode 0x0E) with the controller's feature set.
  `r_lld_con_tx_prog` sets the MD bit of an entry when the one after it is
  pending, which is how *done* reads as "nothing to send here".
- **A link's control structure** (EM 0x400 + link × 90, ADR 0051) names its
  current descriptor at +28: `147e`, entry 9. The same structure holds the
  connection's access address (+12) and CRC initial value (+16) from the
  request, the channel map (+34) and hop increment (+38), and the data
  channel of the event at +22, which moved between events by the hop over
  the map exactly as the specification's first channel selection algorithm
  says: the controller hops, and the medium only has to carry it.

The peripheral had opened the link with its feature request and was
waiting for it to go out; a central's LL_VERSION_IND, delivered, got no
answer staged, because the controller does one procedure at a time.

## Decision

1. **At each connection event the core transmits the link's pending
   descriptor**: from +28 of the link's control structure, if *done* is
   clear, it sends the header and the payload on the Bluetooth air, sets
   *done*, moves +28 to the next entry, and raises the transmit interrupt
   (status bit 1, `r_sch_prog_tx_isr`, ADR 0051) with the event's end.
2. **The header's SN and NESN are the core's**, as they are the hardware's:
   SN toggles with each new packet sent, NESN with each packet received.
3. **A frame on the air for a connection is its header octet, then the
   payload**, as a central's frames to the board already are (ADR 0047):
   within a connection a receiver knows it holds a data channel PDU, and its
   LLID is never the advertising PDU type 0. The data channel is not on the
   frame yet; one link per air has no need of it (ADR 0157, step 2).
4. **Verified by a version exchange**: a central that answers the
   peripheral's feature request and sends LL_VERSION_IND gets the
   controller's own LL_VERSION_IND back, Espressif's company identifier
   (0x02E5) in it. `tests/test_bluetooth_link_layer.py` checks it.

## Consequences

**Good**

- The link carries control procedures both ways; what the controller wants
  to say reaches the central.
- L2CAP, ATT and GATT (ADR 0157 step 3) ride the same descriptors with LLID
  2, so the transport for a Python central to read a peripheral's services
  is now there.

**Costs, accepted**

- Acknowledgement is assumed: a packet sent is a packet received, so
  nothing is retransmitted. The air loses nothing.
- ~~Empty packets are not sent.~~ Since 2026-11-20 (ADR 0169) an event with
  nothing staged sends the empty packet the hardware sends, so a central can
  answer once per event: a central that sent on a timer instead flooded a
  guest running slower than the wall, and its requests queued behind
  hundreds of empty packets.
- Pairing and encryption (step 4) remain: Meshtastic reads and writes its
  characteristics encrypted unless its Bluetooth mode is NO_PIN.
