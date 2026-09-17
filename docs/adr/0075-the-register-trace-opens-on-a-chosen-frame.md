# ADR 0075 — The register trace opens on a chosen frame

**Status:** Accepted — 2026-09-17. Instrument for the wall ADR 0074 left open.

## Context

After message four the station idles for 970 ms and leaves without asking for
an address (ADR 0074). It is not polling a register the device fails to
answer -- the census showed the idle path running evenly -- so it is waiting
for an event that never arrives. A poll can be found from the execution trace,
because the guest keeps reading the register it wants. A missing event cannot:
the guest wrote something, expected a consequence, and got none, and the only
record of that is the register traffic itself.

The WiFi MAC is modelled in C. Its accesses never reach the Python bus, so
`-v` does not show them. The device does carry its own register trace --
`BOARDEMUL_WIFI_TRACE=N` logs the next N accesses with the program counter
that made each -- but the window opens on the **first** frame delivered, which
is a beacon a minute before the handshake, or at start. Neither reaches the
second after message four.

## Decision

**Let the register trace open on the delivery of a data frame of a chosen
length.** `BOARDEMUL_WIFI_TRACE_DATA=187` opens the window when the 187-byte
message three is delivered; `BOARDEMUL_WIFI_TRACE` still sets its length in
accesses. Everything the driver then does -- transmit message four, install
the keys, wait -- falls inside it, with a program counter on every line.

**Stamp each trace line with guest milliseconds.** The window of interest is
one second wide with a known start and end; without a time on the line there
is no way to place an access inside it. The phrase `after the frame` stays
where it is, because the debug scripts match on it.

Nothing about the device's behaviour changes. This is two fields, an
environment variable and a comparison, in the file the project owns.

## Consequences

**Good**

- The one-second window is observable for the first time, from the device's
  side, with attribution to the code that made each access.

- It reuses the trace that already exists rather than adding a second one,
  so the reader learns one line format.

**Costs, accepted**

- **A length is a weak key.** Two different frames of 187 bytes would both open
  the window. Nothing else the peer sends is that length today; if that
  changes, a subtype would be the better trigger.

- **The window is counted in accesses, not time.** How many accesses one
  second holds depends on beacon rate and what the driver is doing; the number
  has to be found by running, and a window too short simply stops early.

## Alternatives rejected

**Log only unmodelled offsets.** The key slots are unmodelled today, but the
question is also what the driver does around them, and a filtered trace would
hide the order. Filtering belongs in the reader, after the fact.

**A verbose bus log.** It cannot see this device; ADR 0011 put the MAC in C
on purpose, and that decision stands.
