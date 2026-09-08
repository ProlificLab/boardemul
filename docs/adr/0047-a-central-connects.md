# ADR 0047 — A central connects

**Status:** Accepted — 2026-09-08.

## Context

ADR 0046 got a packet into the controller: the ROM read the descriptor, saw a
connection request and called its handler, which then rejected it, because the
request's payload was nowhere the handler looked.

The handler says where that is, and it was read off it rather than guessed.
`r_lld_adv_pkt_rx_connect_ind` takes the halfword at **descriptor + 18**,
treats it as an exchange-memory offset, resolves it through the same base
registers as everything else, and copies **thirty-four bytes** from there.

Thirty-four is not an arbitrary number: it is six bytes of initiator address,
six of advertiser, and twenty-two of connection data, which is exactly what
the specification says a connection request carries. The ROM then checks three
of those fields before it will go on -- the interval against six to three
thousand two hundred, the latency against four hundred and ninety-nine, and
the timeout against ten to three thousand two hundred. Those are the
specification's own ranges, which is how the layout was confirmed rather than
assumed.

## Decision

1. **The packet goes where the descriptor already points.** The descriptor
   arrives naming a buffer -- offset `0x7805` in the first case seen -- and
   that is the controller's own choice of where its received bytes live.
   Writing anywhere else would be this emulator choosing for it.

2. **The central is outside the emulator.** The connection request is built by
   whoever is on the air, not by the device model, so what the firmware
   accepts is a packet it was sent rather than one the emulator decided it
   should like.

3. **Acceptance is the firmware's to declare.** Nothing here reports success.
   The evidence is that the link layer starts scheduling a kind of event it
   never schedules while merely advertising, which is the controller saying it
   has a connection to service.

## Consequences

**Good**

- A central connects to an emulated board running unmodified firmware. The
  ROM validates the request, `r_lld_con_start` runs, and the controller begins
  scheduling connection events alongside its advertising.
- The whole chain is covered by a test that takes about a minute: heard
  advertising, request delivered, connection scheduled.

**Costs, accepted**

- **A connection is not a conversation.** Nothing is exchanged over it yet: no
  data channel packets, no channel hopping, no GATT. The link is up and empty.
- The advertiser address in the request is not the board's, and nothing checks
  it, because addresses are not modelled. A real central would have to use the
  address it heard.
- The event kinds the test recognises are two observed constants. They are the
  controller's, not this project's, and a firmware whose link layer schedules
  differently would need them revisited.

## Alternatives rejected

**Have the model declare the connection.** It would pass a test and prove
nothing: the point is that the firmware's own link layer accepted a packet it
was given and changed its behaviour.

**Build the request inside the core.** Simpler to arrange and it would make the
emulator both sender and judge. The central belongs on the air, where anything
else could stand instead.
