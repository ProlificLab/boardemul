# ADR 0122 — A serial line keeps no backlog

**Status:** Accepted — 2026-10-02. Adds a carried QEMU patch (ADR 0017).
Found by ADRs 0120 and 0121, which it lets pass `test_gps_boot`.

**Upstream, 2026-11-18:** Landed upstream (31c98902, 4dcc05c0) in `esp-develop-9.2.2-20261109` as the UART's `rx-overflow` property, off by default, which every run now turns on. A full FIFO loses its oldest byte rather than the newest, and RXFIFO_RST now empties it. The patch is dropped (ADR 0138).

## Context

With SPI and I2C taking their bus time (ADRs 0120, 0121), the vendor
UnitTest build reaches its GPS set-up later in guest time, as on a board, and
then gives up on the L76K: `Wait L76K stop NMEA timeout!`.

Its first step is to send `$PCAS03` with every rate zero and then drain the
line, giving up if bytes are still arriving after three seconds. The module
has streamed since power-on (ADR 0116). Measured (`logs/gps_time_run.py`), it
had sent 4,709 bytes when the command went out, at 17.4 s of guest time.

Every one of those bytes was still waiting. Espressif's UART model
(`hw/char/esp32_uart.c`) never loses a byte:

- `uart_can_receive()` offers only the FIFO's free space, so once the 128-byte
  FIFO is full the socket is not read at all.
- When the FIFO fills, it throttles for one FIFO's worth of byte times at the
  baud rate, so a full queue comes in at 960 bytes a second at 9600 baud.

So the backlog took about five seconds to drain, and the firmware gave up at
three. ADR 0116 passed only because the boot reached the step sooner, with
less queued.

A board keeps no such queue:

- the line carries bytes at the baud rate whether anyone reads them or not;
- the UART keeps what fits in its FIFO, and what does not is lost;
- the hardware says so with `UART_RXFIFO_OVF_INT`.

The model's own header already names that bit (`UART_INT_RAW`, bit 4), but
nothing ever sets it. ESP-IDF's UART driver handles the interrupt by resetting
the FIFO and reporting a `UART_FIFO_OVF` event.

## Decision

1. **`qemu-patch/uart-rx-overflows.patch`: the UART takes the line at its
   baud rate and drops what its FIFO cannot hold.**
   - It accepts up to a FIFO's worth of bytes whenever it is not throttled,
     whether or not the FIFO has room.
   - It keeps what fits and drops the rest.
   - After every delivery it throttles for that many byte times (ten bits
     each), not only when the FIFO is full. So the socket is read at the baud
     rate on the guest's clock, and a burst from a part takes the time it would
     take on a wire.

2. **A dropped byte raises `RXFIFO_OVF`.** It stays raised until the guest
   clears it through `UART_INT_CLR`, as the RX timeout already does. A
   driver's handler then does what it does on a board.

3. **Nothing changes in Python.** A part still writes its output as it
   pleases. The line's pace and its losses are the UART's.

4. **Carried and offered upstream as ADR 0017 asks**, once the user approves.
   It changes how the model treats a socket that sends faster than the line:
   lossy and paced, where it was lossless.

## Consequences

**Good**

- A firmware that ignores a part for a while, then drains the line, finds a
  FIFO's worth of stale bytes, not everything since power-on.
  `test_gps_boot` passes with the boot timeline ADRs 0120 and 0121 give.
- Input arrives at the baud rate in guest time, for every UART.
- A guest that reads too slowly loses bytes and is told, as on silicon.

**Costs, accepted**

- Input pasted into a console UART faster than its baud rate is now lost
  beyond what the FIFO holds, as on a board. Nothing here feeds a console that
  way.
- Before the guest routes a UART's pin, a board receives nothing at all. Here
  the FIFO still fills with the first 128 bytes, and a driver that reads before
  anything resets it gets those. That is one FIFO, not a backlog, and
  ESP-IDF's driver resets the FIFO on every overflow interrupt.
