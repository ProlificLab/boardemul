# ADR 0172 — A part's answers to idle bytes come ahead of the bytes

**Status:** Accepted — 2026-11-20. Extends ADR 0011's SPI link; touches
ADRs 0033 and 0118.

## Context

Meshtastic's on-screen interface asks its own node for the configuration and
asks again every ten seconds of its time until the stream completes
(device-ui `ViewController::runOnce`). Under `-icount` the stream completes
two guest seconds after the request. Without it, on a quiet host, it took 18
and 22: close enough to ten that a busy host makes the interface ask again
before it finishes, the stream starts over, and the screen says "Resync" for
minutes. That is the open item ADR 0164 recorded.

`logs/hot_run.py` counted where the wall time of those seconds went. Almost
all of it was the SD card: 4,000 to 5,000 SPI transactions every two
seconds, each one byte. The firmware reads a block by clocking `0xFF` one
byte at a time — the token, 512 data bytes, the CRC — and every byte is a
duplex transaction, which the controller cannot batch (ADR 0011): it sends
the byte to the Python part and waits for the answer. About 0.4 ms of host
time each, so about a quarter of a second per block. On a board the same
bytes take a microsecond or two each. Under `-icount` the wait costs the
guest no time, which is why the interface is fine there; without it, the
guest's clock is the host's and the wait is the guest's time.

Two things make these exchanges cheap to skip:

- **What an SD card answers to an idle byte is decided before the byte
  comes.** Outside a command frame and a write, `0xFF` changes nothing in
  the card (ADR 0033); the answer is the next byte of what it has queued,
  and during a multiple-block read the next block's (ADR 0118); after that,
  idle.
- **Everything the controller sends goes through one place first**
  (`spi_flush`), so it can say what it did with such bytes before anything
  else reaches the part, in order.

And one thing in the card was slow for no reason: each block's CRC was
computed bit by bit in Python, about four thousand steps a block.
`binascii.crc_hqx` is the same CRC (CCITT, polynomial 0x1021, starting at
zero) in C.

## Decision

1. **A duplex answer carries an offer.** After the reply's bytes, the part
   sends a 16-bit length and that many bytes: what it will answer to the next
   bytes if they are all `0xFF`, on the same select and data/command level,
   with nothing else in between. A part that cannot say sends a length of
   zero; `SpiDevice.ahead(limit)` returns nothing unless a part overrides it.
   An offer is at most 2048 bytes.
2. **The controller answers from the offer.** A duplex transfer of `0xFF`
   bytes that the offer covers is answered from it, with no exchange. The
   transfer's bus time is charged as before (ADR 0120); only the wait for the
   host goes.
3. **The part hears what was used, before anything else.** The first segment
   of any other kind to go out — a write, a duplex the offer does not cover,
   a release, a reset — is preceded by a new segment, *skip* (type 5),
   carrying how many offered bytes were clocked. The part takes them as the
   `0xFF` duplex bytes they were, answers discarded, and ends in the state it
   would have reached byte by byte. Any write-only bytes void what is left of
   an offer, since they may change what the part would answer.
4. **The SD card offers** what is queued, then whole blocks of an open
   multiple-block read, up to the limit; nothing inside a command frame or a
   write. Its block CRC is `binascii.crc_hqx`.

A part may make an offer only if its answers to idle bytes do not depend on
when they come: the offer is made when one exchange ends and used later.

## Consequences

Measured on the same quiet host, Meshtastic 2.7.10 on the T-Deck, without
`-icount`: the SD card's exchanges in the transfer went from about 40,000 to
about 400, and the interface's configuration from 22 guest seconds (18 on a
second run) to 9, completing on its first request. Under `-icount` it takes
2, and the interface was ready 47 s into the run against 26 without, so
`-icount` stays a choice rather than the default. Running the two cores on
two host threads (`BOARDEMUL_TCG_THREADS=multi`) made it 15: the rest of the
gap is the host's speed at the firmware's own code, not its waits.

On a busy host (`logs/dbg/mui_busy.sh`, 14 host CPUs): three T-Decks booted
at once each finished on its first request, in 14 to 16 guest seconds; five
at once asked two or three times and finished 18 to 26 seconds after the
first request. Before, one node on a loaded host looped for minutes.

**Good**

- A block read costs one exchange with Python instead of 515.
- What a part answers, and the state it ends in, is unchanged byte for byte:
  the same answers, given earlier.
- Under `-icount`, where the wait was already free in guest time, a run
  saves the host's time and nothing else.

**Costs, accepted**

- The SPI link protocol changes on both sides together: a controller and a
  Python side from before this cannot talk to each other.
- Two bytes more on every duplex answer, and the part computes an offer it
  may not get to use.
