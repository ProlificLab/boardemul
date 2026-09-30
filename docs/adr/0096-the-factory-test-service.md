# ADR 0096 — The factory-test service on lilygo.local:8896

**Status:** Accepted — 2026-10-01. The service half of ADR 0088's second
workstream, on the TCP transport of ADR 0089. Until now the peer accepted a
connection on any port and only logged what arrived (ADR 0087's observation,
carried over the real transport).

## Context

The whole exchange is now visible on the wire. After NTP (ADR 0085) and the
`lilygo.local` lookup (ADR 0086), the vendor unit-test firmware:

1. opens TCP to `lilygo.local:8896`;
2. sends one JSON object, pretty-printed by cJSON (tabs and newlines):
   `{"SKU": "NA", "product": "T-Deck", "mac": "02:54:44:4D:00:01",
   "date": "2026-10-01,00:21"}` -- the date is local time from NTP, in the
   `CST-8` zone the image names, formatted `%Y-%m-%d,%H:%M`;
3. prints `Send mac address success!`, then `EFUSE MAC: …` and the same JSON;
4. prints `----------REMOVE TASK----------` and deletes the task.

All of this happened against a peer that sends **no application data**, only
TCP acknowledgements. So the firmware needs no reply, and nothing in its
strings reads or parses one: its cJSON strings are about building the object
(`Failed to create the root node`, `…sku_item string node`, `…product string
node`, `…mac string node`, `Failed to convert JSON object to string`). The
published `UnitTest` example does not contain this code at all, so the wire is
the only source for it. It is a one-way registration: the device tells a factory
server what it is and moves on.

ADR 0089 also left one thing to the service layer on purpose: the transport
completes a handshake on **any** port, which a real host does not do. Now that
a service exists, the ports without one can be refused.

## Decision

**Services are chosen by port, and a port with no service refuses the
connection.** A SYN to a port the peer serves gets a SYN-ACK. A SYN to any other
port gets an RST, and so does a segment for a connection the peer does not
have, by RFC 9293's rule for the CLOSED state (§3.10.7.1): a segment carrying an
ACK is answered with `<SEQ=SEG.ACK><CTL=RST>`, and one without an ACK with
`<SEQ=0><ACK=SEG.SEQ+SEG.LEN><CTL=RST,ACK>`, where a SYN counts as one. That is
`emu/tcp.py`'s `refuse`. Port 8896 is the only service; this firmware opens no
other TCP connection.

**The factory-test service records the report and sends nothing back.**
`emu/factory.py` parses the stream as a JSON object once it is complete, and the
peer logs and keeps the registration: SKU, product, reported MAC and date, and
whether the reported MAC is the address the frames came from. A report that
arrives across several segments is reassembled by the transport first. Nothing
is written to the connection, because the device neither needs nor reads
anything and no reply has ever been observed; inventing one would be a guess.

**Teardown follows the client.** When the client closes its half, the service
closes the peer's, so the connection ends in order instead of sitting in
CLOSE_WAIT. A client that never closes keeps its connection; that is its call.

## Consequences

**Good**

- The LAN now has a factory-test server that does what one does with what this
  firmware sends: it takes the report, checks it, and keeps it, and the run's
  log says so in one line.
- The transport behaves like a host with services, not like one that accepts
  everything: a closed port is refused.
- The report parser and the refusal are small pure functions with fast tests;
  the end-to-end test asserts the registration.

**Costs, accepted**

- **No reply is modelled**, because none has been seen. If a real factory
  server does answer, and a future firmware depends on it, that has to be
  observed first.
- The registration lives in the peer's log and memory, not in a file: nothing
  consumes it yet.

## Verification

Two end-to-end runs in the container, concurrently, each passing all five tests
of `test_wifi_connect`, which now runs until the firmware prints `REMOVE TASK`.
In both, the peer logs one line --

    factory test: T-Deck 02:54:44:4D:00:01 (SKU NA) registered at
    2026-10-01,00:32, MAC 02:54:44:4d:00:01 -- the address it transmits from

-- and the firmware prints `Send mac address success!`. The client closes the
connection with a FIN after sending, which the service answers with its own, so
the teardown is the orderly one. No connection was refused: this firmware opens
TCP to port 8896 and nowhere else.

## Alternatives rejected

**Send an acknowledgement, such as `OK` or an echo of the record.** No evidence
for any particular reply, and the firmware demonstrably does not need one.

**Keep accepting any port.** It was the right stand-in while there was no
service layer (ADR 0089). With one, completing a handshake to a port nobody
serves is a host that lies about its listeners.
