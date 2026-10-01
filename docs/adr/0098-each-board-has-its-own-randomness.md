# ADR 0098 — Each board has its own randomness

**Status:** Accepted — 2026-10-01. Amends ADR 0004's decision 4. Found while
chasing why `test_mesh` always ran to its 300-second ceiling.

## Context

`test_mesh` boots two Meshtastic nodes on one air and a third, silent listener,
and is meant to stop as soon as the listener has heard both nodes transmit. It
never did: every run since 2026-09-08 waited out the full 300 s, and still
passed, because its assertions read the nodes' own radio counters rather than
the listener.

**The listener could not tell two senders apart.** It took the sender from four
bytes into each frame, where Meshtastic's header puts `from`. But the air puts
ADR 0037's propagation header in front of the packet (`magic, frequency,
sent_at, …`), so those four bytes were the channel frequency -- `78d00d36`,
906.875 MHz -- the same for both nodes. The listener read frames that way from
the moment it started counting senders (`bdd75e0`, the day after the header was
added). Decoding the packet first fixes it, and the test now asserts that the
listener told two senders apart, so it cannot quietly rot again.

**With that fixed, the listener still heard only one.** A decoded run saw one
`from`, `9a174853`, in 300 s, although both nodes' radios reported transmitting.
A diagnostic run of the same setup saw two, `09ad3208` and `27303fcc`, both at
53 s. So the nodes sometimes transmit **as the same node**, and then each
discards the other's packets as its own. The test's `heard ≥ 1` counts frames
the radio received, not packets the firmware kept, so it passed anyway.

**Why two nodes pick one number.** Both boot from the same configured filesystem
(`make_configured_fs.py`), which persists the node number of the board that
configured it. Meshtastic's `pickNewNodeNum()` keeps a persisted number only if
the node database agrees it belongs to this MAC. It does not (the test gives
each node its own MAC), so each picks a new one with `random()`, seeded from the
hardware RNG. And the hardware RNG is the same on every board: `run.py` passes
QEMU `-seed 1` (ADR 0004), and the ESP32-S3 RNG model reads
`qemu_guest_getrandom`, which under `-seed` is a deterministic stream. Two
boards therefore draw identical "random" numbers, and only diverge when timing
changes how many they have drawn by that point -- which is why they sometimes
collided and sometimes did not.

Two real chips never share a random stream. ADR 0004 pinned the seed so that a
run is reproducible from its inputs. That was right for one board, and was
written before there was a second one to share it with.

## Decision

**Seed each board's hardware RNG from its own identity.** The QEMU seed becomes
the fixed base (1) in the high bits and the board's MAC in the low 48:
`(1 << 48) | MAC`. The MAC is already an explicit, recorded input (the eFuse
blob, ADR 0003), so ADR 0004's rule still holds: a run is determined by its
inputs, and the seed is written to the manifest as before. What changes is that
two boards with different MACs -- which a multi-board run requires anyway, or
they share a node identity (ADR 0036) -- now also have independent randomness,
as real hardware does.

**And stop the mesh run on the evidence its assertions need.** With the listener
fixed, one run stopped at 71 s with both nodes having sent once and neither
having heard anything: their first broadcasts had gone out together, each radio
deaf while transmitting. "Both have spoken" is not "a packet crossed". The run
now also waits for a node's radio to log a reception, and the grace after it
drops from 12 s to 2. To make that visible, the SX1262 model logs one line per
packet received. It also counts the two ways a packet used to vanish without a
trace -- arriving while the radio transmits (`deaf`) or while it is not in
receive mode (`not_listening`) -- and the run manifest carries both.

## Verification

In the container, after all of it: the mesh test passed in 6 runs out of 6 in a
row, each stopping between 57 and 113 s including pytest's own start-up, against
a fixed 301 s before. In an earlier batch of four, three passed at 52–57 s. The
fourth hit the ceiling because one node's firmware never initialized its radio
(zero SPI commands to it in 300 s) -- a boot failure on that board, unrelated to
the air, and one the fixed 300 s run would have failed the same way. It has not
recurred in the ten runs since, and its logs were not kept. Recorded as open.

## Consequences

**Good**

- Two nodes no longer collide on a "random" node number, packet id, or anything
  else a firmware draws from the hardware RNG. The mesh test now proves what it
  claims, and stops as soon as both nodes have spoken, instead of after 300 s.
- A single board is exactly as reproducible as before: same MAC, same seed.

**Costs, accepted**

- **Every run now draws a different random stream than before this change**,
  the default MAC included: the seed was 1 for all boards and is now
  `(1 << 48) | MAC` for each. (An earlier version of this ADR said only
  non-default MACs were affected; that was wrong.) Nothing asserts a particular
  random value, and the one counted-clock suite checked against it -- Bluetooth
  connect, which is deterministic given its inputs -- passes under the new seed,
  2 runs of 2.
- Two boards given the same MAC still share a stream -- and a node identity,
  which is already the thing ADR 0036 says to avoid.

## Alternatives rejected

**Drop `-seed` and use the host's entropy.** Every board would then be
independent, but no run would be reproducible from its inputs, which is what
ADR 0004 exists for.

**Give each node a freshly configured filesystem.** It would avoid the random
fallback in Meshtastic, but not the defect: any firmware that draws from the
hardware RNG on two boards would still get identical numbers.
