# ADR 0105 — The Max's serial parts: a modem and a u-blox receiver

**Status:** Accepted — 2026-10-01. Its QEMU change is a carried patch, so ADR
0017 applies to it.

**Upstream, 2026-11-18:** Its QEMU change, UART2's chardev, landed upstream unchanged (d221471a) in `esp-develop-9.2.2-20261109`; the patch is dropped (ADR 0138).

## Context

The T-Deck Max's factory demo, heard through its USB port (ADR 0100), gave up
on two parts:

    GPS Connect failed~!
    [A7682E] Init Fail

The board model had put ADR 0052's L76K receiver on the machine's second UART,
"the same receiver on the same UART as the other board", and noted that nothing
had ever exercised it. Both halves of that were wrong for this board.

The vendor's pin map for this image (`lib/TDeckMaxBoard/src/TDeckMaxBoard.h`,
software `v3.1-260313`) says:

    #define SerialAT  Serial1      // A7682E modem, RXD 10, TXD 11
    #define SerialGPS Serial2      // GPS, RXD 2, TXD 16

and its hardware folder carries the receiver's datasheet: a u-blox MIA-M10Q.

The demo's sources say how each is spoken to (`examples/factory`):

- **The receiver** is opened at 38400, then 9600. At each rate the demo sends
  three UBX `CFG-CFG` messages and then a `CFG-RATE` *poll* (no payload). It
  waits up to 800 ms for a frame whose class and id are `CFG-RATE` itself:
  the answer to the poll, not an acknowledgement. After that it only listens,
  feeding every byte to TinyGPS++ for position, date, time, satellites and
  speed.
- **The modem** is powered through the expander's PWRKEY line and asked `AT`
  up to five times, a second each, until it answers `OK`. Then it is sent
  `AT+CLIP=1`.

There was a further obstacle under both. **The pinned fork never connects the
S3's third UART to a character device.** The two lines that would are
commented out in `hw/xtensa/esp32s3.c`, so `Serial2` reaches nothing whatever
is modelled.

## Decision

1. **`qemu-patch/uart2-chardev.patch`** restores those two lines, so UART2 is
   `-serial`'s third port, as UART0 and UART1 are its first two. Reported as
   espressif/qemu#186 and offered as #187, the same two lines against
   `esp-develop`, from ProlificLab/qemu. Carried until it lands.

2. **The receiver is a u-blox M10 (`UBloxM10` in `emu/devices/gps.py`) on the
   third UART**, built in and always fitted, as it is on this board:
   - it streams GGA and RMC once a second of the board's time (ADR 0104),
     from power-on, because this firmware never speaks first;
   - indoors (no `--gps`) it streams with no fix and no time; with `--gps` it
     has a fix there;
   - UBX configuration is acknowledged with `ACK-ACK`. A poll is answered with
     the configuration it names, then acknowledged. Only `CFG-RATE` is known
     (1000 ms, one per solution, GPS time), the only poll any firmware here
     sends. Others are logged.

3. **The modem is an A7682E (`emu/devices/modem.py`) on the second UART**,
   always fitted. It echoes, as it does by default, and answers `AT`, `ATE0/1`
   and `AT+CLIP`. Anything else is answered `ERROR` and logged as unmodelled,
   so the next command to model comes off a run. There is no SIM yet.

4. **The original T-Deck is unchanged.** Its receiver is an add-on L76K on its
   second UART, measured there by ADR 0052, and it still waits to be spoken to.

## Consequences

**Good**

- The factory demo finds its receiver (`GPS Task Create...!`) and its modem.

**Costs, accepted**

- A seventh carried patch, though a two-line one.
- The modem answers only what has been asked of it so far. A firmware that
  goes further will see `ERROR`s, and the run will list them.
