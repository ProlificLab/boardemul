# ADR 0118 — The card streams a multiple-block transfer

**Status:** Accepted — 2026-10-02. Extends ADR 0033.

## Context

A timed Meshtastic node (ADR 0113) spends one second of its guest time, about
10 to 11 s in, at 170 to 210 ms of wall time per millisecond: three and a half
minutes for one second. Counting socket reads per server thread
(`logs/hot_run.py`) put all of it on the SD card: about 350,000 one-byte
transfers in that second, each `0xFF` out and `0xFF` back. The commands just
before were `CMD18` (READ_MULTIPLE_BLOCK) and `CMD12` (STOP_TRANSMISSION).

The model answers `CMD18` with one block. A card does not stop there. ChaN's
*How to Use MMC/SDC* (elm-chan.org/docs/mmc/mmc_e.html) describes it:
- `CMD18` reads blocks in sequence from the address given, and "the read
  operation continues as open-ended";
- the host ends it with `CMD12`;
- the byte that arrives right after `CMD12` is a stuff byte, which the host
  discards before taking the response.

The host is arduino-esp32's SD library. The build links ESP-IDF v4.4.7 and
carries that library's log strings ("Select Failed", "no token received"), so
it is the 2.0 series. Its `sd_diskio.cpp` (2.0.17) says what it does with a
card like this one:

- **`sdReadSectors` reads any run of more than one sector with `CMD18`.** It
  polls for each block's `0xFE` token for up to 500 ms. If one never comes it
  stops the transfer, then starts again at the sector that failed. So against
  the model, every sector after the first costs 500 ms of polling.
- **It stops a read with `CMD12` sent as seven bytes**, the seventh being the
  stuff byte. It then looks for the R1 in the next nine.
- **`sdWriteSectors` writes a run with `ACMD23` and then `CMD25`.** Each block
  goes out behind the token `0xFC`, and the run ends with `0xFD`. The model
  refuses `CMD25` as illegal.
- **`sdWriteBytes` reads the data response from the first byte after the
  block's CRC** (`transfer(0xFF) & 0x1F`). The model puts an idle byte there.
  A single-block write survived that only because the host then asks for the
  card's status and believes it. A multiple-block write requires `0x05` there
  and would retry.

ChaN describes the write side the same way:
- `CMD25` takes one or more data packets until a Stop Tran token;
- `0xFC` starts each packet and `0xFD` ends the transfer;
- the card signals busy after every block and after the stop token;
- `ACMD23` only names how many sectors the card may erase ahead.

## Decision

1. **`CMD18` opens a read that streams.** Each block comes as the usual gap,
   `0xFE`, 512 bytes and the CRC. The next block follows for as long as the
   host keeps clocking. Block numbers run on from the one asked for. A block
   past the end of the card reads as zeros, as a single read does now.

2. **`CMD12` ends it.** Whatever of the stream had not been clocked out yet is
   dropped, and the R1 follows the stuff byte. The response gap's first byte
   is that stuff byte.

3. **`CMD25` opens a write that takes packets until `0xFD`.**
   - Each packet starts with `0xFC`. It is answered with the data response
     `0x05`, then busy.
   - `0xFD` ends the write, followed by busy.
   - `ACMD23` is accepted. It only lets a card erase ahead, which a card held
     in memory has no use for.

4. **A data response comes in the first byte after the CRC, for `CMD24` as
   well as `CMD25`**, then two bytes of busy (`0x00`), then idle. That is the
   byte the host reads it from. The idle byte the model put first had no
   source.

## Consequences

**Good**

- A run of sectors is read in one transfer, as on a board, instead of costing
  500 ms of polling per sector after the first. The firmware's start-up
  timeline loses those seconds, so what it does next happens when it would on
  silicon.
- The timed mesh's slowest second goes away, and with it about three and a
  half minutes of wall time per timed node.
- A firmware that writes a run of sectors can now do it. Before, `CMD25` was
  refused and the write failed.

**Costs, accepted**

- A host that never sends `CMD12` would be streamed blocks for ever, which is
  what a card does too.
- `ACMD22`, the count of blocks written, is still not modelled. The host
  asks for it only after a data response saying a write failed, and this card
  reports none.

## Alternatives rejected

**Answer `CMD18` with a fixed number of blocks.** The card does not know how
many the host wants, and neither does the command.
