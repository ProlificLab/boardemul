# ADR 0069 — The transmitted frame is where the DMA descriptor points

**Status:** Accepted — 2026-09-17. Corrects a detail of ADR 0059's transmit
read that only management frames ever exercised.

## Context

ADR 0068 got the handshake's first message to the supplicant, and the supplicant
answered: traced end to end, the station builds message two and hands it to the
transmit engine (`ppTxProtoProc` → `lmacTxFrame` → `lmacSetTxFrame`), which arms
slot zero exactly as it does for a management frame. The device reads the armed
slot's frame and passes it to the Python peer.

The peer saw **garbage**: about a quarter of the reads had an impossible 802.11
protocol version, the rest had random addresses and a constant where the third
address belongs. For a while this looked like the device inventing transmits
out of stale memory. It was not. Dumping the bytes showed every one of them
carried, eighteen bytes in, a well-formed To-DS data frame from the station:

    08 01 00 00 | 02 00 00 00 00 01 | 02 54 44 4d 00 01 | 02 00 00 00 00 01 ...
    (data, To-DS; BSSID; the station; BSSID)

Message two was real, correctly built, and being **read eighteen bytes early**.

**The read used the wrong pointer.** `wifi_read_tx_frame` took the frame from
the pointer at `EB_FRAME` (offset sixteen in the eb). Dumping the whole eb for
a management frame and a data frame side by side showed why that ever worked:

| | management (probe) | data (message two) |
| --- | --- | --- |
| `EB_FRAME` (eb+16) | buffer+96 = the frame | buffer+96, **18 before** the frame |
| DMA descriptor's buffer word | buffer+96 = the frame | buffer+114 = the frame |
| descriptor length | 59 = head+body | 157 = head+body |

The eb holds, at offset four, a pointer to a standard ESP `lldesc` -- the
descriptor the MAC hardware transmits by DMA. Its second word is the frame's
address and its first word carries the length in bits twelve to twenty-three.
For a management frame `EB_FRAME` and the descriptor's buffer coincide, so
reading `EB_FRAME` had worked for every probe, authentication and association
since ADR 0059. A data frame reserves eighteen bytes of headroom that
`EB_FRAME` points into, and only the descriptor points past it to the frame.

## Decision

**Read the transmitted frame from the DMA descriptor, not from `EB_FRAME`.**
Follow the descriptor pointer at eb offset four, take the frame address from the
descriptor's second word, and fall back to `EB_FRAME` only if there is no
descriptor. This is the address the real MAC reads, so it is right for both
frame kinds by construction rather than by coincidence, which is what ADR 0006
asks for.

## Consequences

**Good**

- Message two is read correctly. Measured on the same run that had zero before:
  **two hundred and twenty-three** EAPOL replies now recognised, where the old
  read produced only rejected garbage.

- The "device reports phantom transmits" note in the WiFi state is retired.
  They were never phantom; they were message two, read at the wrong offset. A
  quarter passed a data-frame type check by chance, which is how they came to
  be believed as the station answering (they were not that either -- the real
  answer was invisible under the misread).

- Management frames are unaffected, because the descriptor's buffer address
  equals `EB_FRAME` for them. The scan and the connect suites confirm it.

**Costs, accepted**

- **A third reach into driver memory**, after the channel out of `wDevCtrl+44`
  and the frame out of the buffer. It is the same shortcut ADR 0064 accepted
  and named, and if anything more honest than the old one: the descriptor is
  exactly what a real MAC's DMA engine consumes.

- **Only the first descriptor is followed.** A frame that spanned a chain of
  `lldesc` nodes would need the chain walked; the handshake's frames fit one
  node and this reads one. Recorded so the first multi-node frame has somewhere
  to look.

- **It exposes the next wall rather than removing the last.** With message two
  read correctly the peer still does not answer it: the integrity code does not
  verify, so no message three goes out and the station still times out with
  reason fifteen. That is a separate problem, in the key schedule or the code
  computed over the frame, and it is now the only thing between here and a
  completed handshake.

## Alternatives rejected

**Skip eighteen bytes for a data frame.** The offset is not stored anywhere in
the eb and eighteen is not a constant of the protocol -- it is whatever
headroom this frame happened to reserve. Hardcoding it would break the moment
the headroom changed, and it would say nothing true about the hardware.

**Search the buffer for a valid frame control.** A frame's first octet is only
weakly distinguishable from data, and ADR 0068 already paid for reading a count
without reading the frame. The descriptor removes the guessing entirely.
